"""正文内容规范（Content Spec）：**分类的唯一真相源，也是给 LLM 的硬约束。**

为什么需要这一层：

- 规则分类器里散着一堆关键词、先验、阈值，LLM 提示词里又要再写一遍 —— 两套标准一定会漂移；
- 更糟的是"模型补全"：给它一段文本问"这属于哪类"，它会给你一个听起来合理的答案，
  即使这段根本不是五类里任何一种。**没有边界约束的分类，等于没有分类。**
- 所以规范必须是可执行的：每一类都写清"什么情况下才能归到我这"、"必须带什么证据"、
  "什么情况必须拒绝"。分类器按它判，LLM 按它答，自检按它验。

五类 + 一个 `unclassified`（允许模型说"我不知道"，比强行归类强）。

每类的四件事：
1. `boundary`  块的边界（一个块最多承载什么）
2. `evidence`  归到这一类必须能指出的证据（正则/结构特征，任一条成立）
3. `forbid`    出现这些特征就**不能**归到这一类（用来挡住"看着像"的误判）
4. `payload`   归一之后必须落成的结构化产物（没有产物说明归类没意义）
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 词汇/特征（供规则与校验共用）

STATBLOCK = re.compile(r"^\s*(STR|CON|SIZ|DEX|INT|POW|APP|EDU)\b", re.M)
DICE = re.compile(r"\b\d*[dD]\d+\b")
STEP = re.compile(r"^\s*(?:\d{1,2}[.、)]|步骤\s*\d|第[一二三四五六七八九十]+步)", re.M)
PRICE = re.compile(r"[$￥]\s*\d|\d+\s*(?:美分|美元|便士|先令|英镑)")
PCT_SKILL = re.compile(r"[（(]\s*\d{1,3}\s*%\s*[）)]")
RULE_WORDS = ("检定", "掷骰", "难度等级", "奖励骰", "惩罚骰", "孤注一掷", "对抗", "回合", "判定")
PC_WORDS = ("属性", "技能点", "职业", "背景", "角色卡", "信用评级", "调查员")
TOOL_WORDS = ("价格", "武器", "装备", "物品", "载具", "弹数", "护甲值")
NPC_WORDS = ("护甲", "理智损失", "特殊能力", "栖息地", "攻击", "神话生物", "邪神")
LORE_WORDS = ("神话", "历史", "传说", "信仰", "典籍", "世界", "时代", "仪式")


@dataclass(frozen=True)
class KindSpec:
    key: str
    zh: str
    boundary: str
    evidence_note: str
    forbid_note: str
    payload: str

    def evidence(self, text: str, meta: dict) -> list[str]:
        """返回命中的证据列表（空 = 不能归到这一类）。

        证据有三类，**结构证据和文本证据等价**：
        1. 文本特征（关键词 / 骰子 / 价格）；
        2. 版式签名（表格行、清单行、属性块）；
        3. 归属证据（章节语义、父条目继承）—— 一个块自身没有关键词时，
           它所在的"创建调查员"章仍然足以说明它是玩家属性类。
        """
        hits: list[str] = []
        t = text or ""
        section = (meta or {}).get("section") or ""
        chapter = (meta or {}).get("chapter") or ""
        ctx = f"{chapter} {section}"

        if (meta or {}).get("inherited_kind") == self.key:
            hits.append("父条目继承")
        if (meta or {}).get("force_kind") == self.key:
            hits.append("切分阶段版式判定")
        if self.key == "rule":
            hits += [w for w in RULE_WORDS if w in t][:3]
            if STEP.search(t):
                hits.append("编号步骤")
            if DICE.search(t):
                hits.append("骰子表达式")
        elif self.key == "pc":
            hits += [w for w in PC_WORDS if w in t][:3]
            if "角色卡" in t:
                hits.append("角色卡")
            if re.search(r"(创建调查员|技能|职业)", ctx):
                hits.append(f"章节归属（{ctx.strip()}）")
        elif self.key == "tool":
            if meta.get("list_item") or meta.get("row"):
                hits.append("清单/表格行")
            if re.search(r"(物价|装备|武器|物品|道具)", ctx):
                hits.append(f"章节归属（{ctx.strip()}）")
            if PRICE.search(t):
                hits.append("价格")
            hits += [w for w in TOOL_WORDS if w in t and w != "价格"][:2]
        elif self.key == "npc":
            if STATBLOCK.search(t) or ("平均" in t and "掷骰" in t):
                hits.append("属性块")
            hits += [w for w in NPC_WORDS if w in t][:2]
            if re.search(r"(怪物|邪神|妖魔|野兽|神话生物)", ctx):
                hits.append(f"章节归属（{ctx.strip()}）")
        elif self.key == "lore":
            hits += [w for w in LORE_WORDS if w in t][:3]
            # 叙述性段落本身就是世界观的载体：有句末标点、没有属性块、没有价格。
            # 阈值放到 60 字：章首导语往往只有一两句，但它确实是世界观。
            if len(t) > 60 and not STATBLOCK.search(t) and not PRICE.search(t):
                ends = sum(t.count(ch) for ch in "。！？")
                if ends >= 1 and ends / max(1, len(t)) * 100 >= 1.2:
                    hits.append("叙述性段落")
            # 章节归属：爱手艺生平、神话、主持技巧、典籍、模组 → 都是叙事/设定
            if re.search(r"(爱手艺|克苏鲁神话|主持游戏|典籍|介绍|模组)", ctx):
                hits.append(f"章节归属（{ctx.strip()}）")
        return hits

    def forbidden(self, text: str, meta: dict) -> str:
        """返回命中的"禁止特征"（非空 = 不许归到这一类）。"""
        t = text or ""
        if self.key == "tool" and STATBLOCK.search(t):
            return "含属性块（那是 NPC）"
        if self.key == "npc" and not (STATBLOCK.search(t) or "平均" in t or "特殊能力" in t):
            if (meta or {}).get("inherited_kind") == "npc" or (meta or {}).get("force_kind") == "npc":
                return ""
            if len(t) > 400:
                return "长叙述但无属性/能力块"
        if self.key == "rule" and not self.evidence(t, meta) and len(t) > 200:
            return "无判定特征"
        return ""
        # 注：以下不可达，仅为保留结构
        return ""


SPEC: dict[str, KindSpec] = {
    "rule": KindSpec(
        "rule", "规则类",
        "一个块只承载**一条规则或一个判定点**；跨规则的步骤要拆开，判定分支留在同一块内",
        "出现 检定/掷骰/难度等级/奖励骰/回合 等判定词，或有编号步骤/骰子表达式",
        "纯叙事、纯设定、无任何可执行条件",
        "rule 实体 + 原文 fact + 子类 fact（战斗/追逐/理智/魔法…）+ 可选流程图",
    ),
    "pc": KindSpec(
        "pc", "玩家属性类",
        "一个块只描述**角色自身**的东西：属性、派生值、技能、职业、背景、状态",
        "出现 属性/技能点/职业/角色卡/信用评级 等角色词",
        "描述外部世界、物品、怪物",
        "pc 作用域字段（基础属性、派生值）+ 技能实体（名称/基础值/对抗技能）",
    ),
    "tool": KindSpec(
        "tool", "工具类（物品/道具/装备）",
        "**一行/一物一块**：清单行与表格行必须独立成块，名字即索引",
        "清单行、表格行，或含 价格/伤害/射程/弹数 等物品列",
        "含属性块（STR/CON…）—— 那是 NPC",
        "item 实体（名称唯一）+ 字段字典（价格/伤害/射程…，列名即字段名）",
    ),
    "npc": KindSpec(
        "npc", "NPC 类（怪物/人）",
        "**一只一块**：名字 + 属性块 + 能力/攻击描述；不得混入其他 NPC",
        "属性块（STR/CON/SIZ…+平均/掷骰）、护甲/理智损失/特殊能力",
        "长叙述但没有任何属性或能力块（那是世界观/剧情）",
        "npc / monster 实体（只读）+ 属性字段 + 能力字段",
    ),
    "lore": KindSpec(
        "lore", "常识与世界观类",
        "**一个主题一块**：叙事/设定/历史/地点/人物；相似主题合并到同一 topic",
        "出现 神话/历史/传说/信仰/典籍/时代 等设定词，或本身就是叙事段落",
        "可由规则/属性/物品/怪物完整解释的内容",
        "lore 实体（摘要 + 原文 + 补充原文）",
    ),
    "unclassified": KindSpec(
        "unclassified", "未分类",
        "无法归入以上五类时的落点：**宁可留空，也不要硬塞**",
        "无（这是拒绝位，不是类别）",
        "把无法判定的内容塞进 lore 充数",
        "无；在报告里列出来供人工或 LLM 复核",
    ),
}

KINDS = [k for k in SPEC if k != "unclassified"]

KIND_ZH = {k: v.zh for k, v in SPEC.items()}


def spec_text() -> str:
    """规范的人读版（也是喂给 LLM 的那段文字，两边用同一份）。"""
    lines = ["# 正文内容规范（硬约束）", ""]
    for k in KINDS:
        s = SPEC[k]
        lines += [
            f"## {k} · {s.zh}",
            f"- 边界：{s.boundary}",
            f"- 证据（必须至少命中一条）：{s.evidence_note}",
            f"- 禁止：{s.forbid_note}",
            f"- 必须产出的结构化内容：{s.payload}",
            "",
        ]
    lines += [
        "## 通用硬约束",
        "1. 只能从以上五类中选择；无法判断时选 `unclassified`，**禁止猜测**。",
        "2. 每个结论必须给出原文证据片段（`evidence`，逐字摘自原文，不得改写）。",
        "3. 块的边界不得跨越两条规则 / 两只 NPC / 两个物品条目。",
        "4. 版权、目录、译者名单等非游戏内容不参与分类（已在编译入口拦截）。",
    ]
    return "\n".join(lines)


def structural_forbidden(kind: str, text: str, meta: dict | None = None) -> str:
    """LLM 判别路径专用的禁忌项：**只挡"结构上自相矛盾"的归类，不做典型性检查。**

    与 KindSpec.forbidden 的分工：
    - forbidden() 里的 rule「无判定特征」（那张 CoC 关键词表）和 npc「长叙述但无
      属性块」本质是典型性检查 —— 给规则打分器兜底用的。模型读完整个块之后说
      "这是规则"，比"这段没命中词表"更可信；拿它否决 LLM 等于规则又一次压过
      模型（mock 实测过一轮：19 块判 0 采纳，就是这类检查干的）。
    - LLM 路径只挡真正结构性的矛盾。目前明确的一条：tool 块里出现 STR/CON 属性
      块 —— 怪物的身体不可能是一件装备，这与换什么书无关。
    """
    if kind == "tool" and STATBLOCK.search(text or ""):
        return "含属性块（那是 NPC）"
    return ""


def validate(kind: str, text: str, meta: dict | None = None) -> tuple[bool, list[str]]:
    """按规范校验一次归类。返回 (是否合规, 违规说明)。

    这是"硬约束"的落地：分类器说完不算数，得能通过这里的检查。
    """
    meta = meta or {}
    if kind not in SPEC:
        return False, [f"未知类别 {kind}"]
    if kind == "unclassified":
        return True, []
    s = SPEC[kind]
    problems: list[str] = []
    if not s.evidence(text, meta):
        problems.append(f"缺少「{s.zh}」的证据特征")
    bad = s.forbidden(text, meta)
    if bad:
        problems.append(bad)
    # 通用：条目类必须"一块一条"，一条以上说明分类前就该切
    if kind in ("tool", "npc") and len(PCT_SKILL.findall(text or "")) >= 2:
        problems.append("块内含多个条目（应在切分阶段拆开）")
    return (not problems), problems


# ---------------------------------------------------------------- LLM 契约

CLASSIFY_SCHEMA = {
    "type": "object",
    "required": ["kind", "subtype", "evidence", "confidence"],
    "properties": {
        "kind": {"enum": KINDS + ["unclassified"]},
        "subtype": {"type": "string", "description": "子类；无合适词时填 general"},
        "evidence": {"type": "string", "description": "逐字摘自原文的证据片段"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "description": "一句话说明为什么符合该类的边界"},
    },
    "additionalProperties": False,
}


def build_prompt(text: str, section_path: str = "", budget: int = 700) -> str:
    """生成分类提示词：**规范全文 + 待判文本 + 输出契约**。

    三个刻意的设计：
    - 规范放在最前面，且是"边界/证据/禁止"三项，模型没法只靠关键词联想；
    - 允许 `unclassified`，并明说"猜错比留空更糟"；
    - 要求 `evidence` 逐字摘录 —— 编造的证据会和原文对不上，可在校验时直接发现。
    """
    body = (text or "").strip()[:budget]
    return (
        f"{spec_text()}\n\n"
        f"## 待判内容\n章节路径：{section_path or '(未知)'}\n```\n{body}\n```\n\n"
        "## 输出\n只输出一个 JSON，字段见下，不要解释：\n"
        f"{CLASSIFY_SCHEMA}\n"
    )


def check_llm_verdict(verdict: dict, text: str, meta: dict | None = None) -> tuple[dict, list[str]]:
    """校验 LLM 的答案：类别合法、证据能在原文里找到、符合该类边界。

    任何一条不过 → 降级为 `unclassified` 并记录原因。**不修补它的答案**，
    因为"帮模型圆场"正是分类漂移的来源。
    """
    problems: list[str] = []
    kind = str(verdict.get("kind") or "").strip()
    if kind not in SPEC:
        return {"kind": "unclassified", "subtype": "", "evidence": "", "confidence": 0.0}, [f"非法类别 {kind!r}"]
    ev = str(verdict.get("evidence") or "").strip()
    flat = re.sub(r"\s+", "", text or "")
    if ev and re.sub(r"\s+", "", ev) not in flat:
        problems.append("evidence 无法在原文中逐字找到（疑似编造）")
    ok, probs = validate(kind, text, meta)
    problems += probs
    if problems:
        return {"kind": "unclassified", "subtype": "", "evidence": ev, "confidence": 0.0}, problems
    return verdict, []
