"""五大类判定：规则类 / 玩家属性类 / 工具类 / NPC类 / 常识与世界观类。

判定的思路是**先看这本书怎么排的，再看这一块写了什么**：

1. **章节先验（strong）**：规则书是按主题编章的 —— 第四章"技能"整章都是玩家属性类，
   第六章"战斗"整章都是规则类，第十四章"怪物"整章都是 NPC 类。一页有 100 个词，
   靠词频投票会把"战斗"章里的怪物数据投成 NPC 类，而章节先验几乎不会错。
2. **块的写法特征（medium）**：属性块长得太有辨识度了 —— 表头"属性/平均/掷骰" +
   STR/CON/SIZ 那几行；物品表长得是"名称/伤害/价格"；规则段满是"检定/难度等级/若…则"。
3. **子类从内容里长**：不写死"战斗/追逐/理智"三选一。规则块读到的领域词（战斗、追逐、
   理智、魔法、伤害、毒、老化…）就是它的子类；出现一个没见过的领域词，就并排新增一个
   子类。这正是需求里说的"每当出现一个新的类似规则的类就添加为一个并行的子类"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import spec as spec_mod

KINDS = ("rule", "pc", "tool", "npc", "lore")

KIND_ZH = {
    "rule": "规则类",
    "pc": "玩家属性类",
    "tool": "工具类",
    "npc": "NPC类",
    "lore": "常识与世界观类",
    "": "未分类",
}

# ---------------------------------------------------------------- 词汇表
# 这些**不是**字段预设，只是判定用的语言特征。字段在 schema_skill 里另外长。

KW_RULE = {
    "检定": 3, "掷骰": 3, "难度等级": 3, "大失败": 3, "奖励骰": 3, "惩罚骰": 3,
    "孤注一掷": 3, "对抗": 2, "回合": 2, "目标": 1, "成功": 1, "失败": 2, "规则": 1,
    "判定": 2, "若": 1, "否则": 2, "花费": 1, "可以": 1, "必须": 1,
}
KW_PC = {
    "调查员": 2, "属性": 3, "技能点": 3, "职业": 2, "背景": 2, "角色卡": 3,
    "力量": 2, "敏捷": 2, "意志": 2, "教育": 2, "体型": 2, "智力": 2, "外貌": 2,
    "信用评级": 2, "年龄": 1, "技能": 1, "专攻": 2,
}
KW_TOOL = {
    "价格": 3, "武器": 3, "装备": 3, "物品": 2, "道具": 2, "载具": 3,
    "购买": 2, "弹药": 2, "护具": 2, "工具": 2, "花费": 1, "美元": 2,
}
KW_NPC = {
    "怪物": 3, "神话生物": 3, "邪神": 3, "外神": 3, "旧日支配者": 3,
    "护甲": 2, "理智损失": 3, "特殊能力": 2, "栖息地": 2, "攻击": 1, "HP": 2,
}
KW_LORE = {
    "神话": 2, "洛夫克拉夫特": 2, "历史": 2, "传说": 2, "信仰": 2, "典籍": 2,
    "世界": 1, "神": 1, "时代": 1, "禁忌": 2, "知识": 1, "仪式": 1,
}

# 章节标题 → 类别（先验）。命中不了就返回 None，不硬猜。
TITLE_PRIOR: list[tuple[str, str]] = [
    ("战斗", "rule"), ("追逐", "rule"), ("理智", "rule"), ("魔法", "rule"),
    ("游戏系统", "rule"), ("技能", "pc"), ("创建调查员", "pc"), ("调查员", "pc"),
    ("法术", "rule"), ("怪物", "npc"), ("野兽", "npc"), ("诸神", "npc"),
    ("外星科技", "tool"), ("附录", "tool"), ("物价", "tool"), ("武器", "tool"),
    ("介绍", "lore"), ("前言", "lore"), ("爱手艺", "lore"), ("主持游戏", "lore"),
    ("典籍", "lore"), ("模组", "lore"), ("术语", "lore"), ("译名", "lore"),
    ("使用说明", "lore"),
]

# 规则子类的"领域词"。只用于**命名**子类，不限制子类数量 —— 读不到这些词时，
# 会退化成用章节标题本身当子类名（例如"6.3 战技" → 战技）。
RULE_DOMAINS = [
    "战斗", "追逐", "理智", "魔法", "射击", "伤害", "治疗", "毒", "疾病",
    "老化", "训练", "经验", "幸运", "技能检定", "对抗", "战技", "护甲", "载具",
    "召唤", "束缚", "传送", "占卜", "交涉", "潜行", "探索", "掉落", "溺水", "火焰",
]

_STATBLOCK_RE = re.compile(r"^(STR|CON|SIZ|DEX|INT|POW|APP|EDU)\b", re.M)
_STATHEAD_RE = re.compile(r"(属性\s*\|\s*平均|平均\s*\|\s*掷骰|属性\s+平均\s+掷骰)")
_DICE_RE = re.compile(r"\b\d*[dD]\d+\b")
_NUM_LABEL_RE = re.compile(r"^[^\n：:]{1,12}[：:]\s*\S", re.M)


@dataclass
class Verdict:
    kind: str
    subtype: str
    confidence: float
    scores: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


def title_prior(*titles: str) -> tuple[str | None, str | None]:
    """返回 (类别, 命中的标题词)。`titles` 从最具体到最宽（小节 → 章 → 书）。"""
    for t in titles:
        if not t:
            continue
        for word, kind in TITLE_PRIOR:
            if word in t:
                return kind, word
    return None, None


def _kw_score(text: str, lex: dict[str, int]) -> tuple[float, list[str]]:
    score, hits = 0.0, []
    for w, weight in lex.items():
        n = text.count(w)
        if n:
            score += weight * min(n, 3)
            hits.append(f"{w}×{n}")
    return score, hits


def discover_subtype(kind: str, block_title: str, text: str, section_title: str) -> str:
    """子类不是写死的枚举，而是从内容里读出来的词。

    - 规则类：先看正文命中了哪个领域词（战斗/追逐/理智…），再看章节标题；
    - 其余类：用"去掉编号的章节标题"当子类，空洞的（"技能列表"）退化成 general。
    """
    hay = f"{block_title}\n{text[:400]}"
    if kind == "rule":
        best, best_n = "", 0
        for d in RULE_DOMAINS:
            n = hay.count(d)
            if n > best_n:
                best, best_n = d, n
        if best:
            return best
        return _clean_title(section_title) or "general"
    if kind == "pc":
        for d in ("状态", "技能", "职业", "背景", "属性", "关系", "年龄", "信用"):
            if d in hay:
                return d
        return "属性"
    if kind == "tool":
        for d in ("武器", "载具", "护甲", "弹药", "服务", "物价", "典籍", "科技"):
            if d in hay:
                return d
        return "物品"
    if kind == "npc":
        for d in ("邪神", "外神", "旧日支配者", "神话生物", "野兽", "人类"):
            if d in hay:
                return d
        return "怪物"
    if kind == "lore":
        for d in ("神话", "历史", "地点", "人物", "信仰", "典籍", "模组", "时代"):
            if d in hay:
                return d
    return "general"


def _clean_title(t: str) -> str:
    t = re.sub(r"^第[〇零一二三四五六七八九十百0-9]+[章篇部]\s*", "", t or "")
    t = re.sub(r"^\d+(\.\d+)*\s*", "", t)
    t = re.sub(r"[（(].*?[）)]", "", t)
    return t.strip(" ：:·-")[:12]


def classify(block, *, chapter_title: str = "", section_title: str = "") -> Verdict:
    """给一块定类别。`block` 只要有 .title/.text/.tables/.meta 就够（协议式调用）。"""
    text = block.text or ""
    title = block.title or ""
    meta = getattr(block, "meta", {}) or {}

    # 切分阶段已经确定的类别（清单条目这类版式自带类别，不需要再投票）
    if meta.get("force_kind"):
        return Verdict(
            kind=meta["force_kind"],
            subtype=meta.get("force_subtype", ""),
            confidence=0.95,
            scores={meta["force_kind"]: 9.0},
            evidence=["切分阶段按版式定为清单条目"],
        )

    scores = {k: 0.0 for k in KINDS}
    evidence: list[str] = []

    # 1) 章节先验：权重 5，一个词就能压过零散的词频
    prior, prior_word = title_prior(section_title, chapter_title)
    if prior:
        scores[prior] += 5.0
        evidence.append(f"章节先验「{prior_word}」→ {prior}")

    # 2) 词汇特征
    for kind, lex in (("rule", KW_RULE), ("pc", KW_PC), ("tool", KW_TOOL), ("npc", KW_NPC), ("lore", KW_LORE)):
        s, hits = _kw_score(text[:1200], lex)
        scores[kind] += min(s, 9) * 0.45  # 封顶：避免长文本靠堆词赢
        if hits:
            evidence.append(f"{kind}:{','.join(hits[:3])}")

    # 3) 版式签名（最硬的结构证据）
    if meta.get("is_table"):
        header = " ".join(meta.get("header") or [])
        if re.search(r"名称|物品|武器|载具|弹药", header) and re.search(r"伤害|价格|射程|弹数", header):
            scores["tool"] += 6
            evidence.append("表格签名：名称+伤害/价格 → 工具类")
        elif re.search(r"消耗|POW|理智|法术", header):
            scores["rule"] += 4
            evidence.append("表格签名：消耗/POW → 规则类")
    if _STATBLOCK_RE.search(text) and (_STATHEAD_RE.search(text) or "平均" in text):
        scores["npc"] += 7
        evidence.append("属性块签名：STR/CON/SIZ… + 平均/掷骰")
    if _DICE_RE.search(text):
        scores["rule"] += 1.2
    if len(_NUM_LABEL_RE.findall(text)) >= 3:
        scores["npc"] += 1.0  # "HP：14 / 移动：5" 这种多标签行更像实体卡

    # 候选排序后**逐条过规范**：第一个能通过硬约束的才算；一个都过不了就是 unclassified。
    # 这一步是"分类不是模型说了算"的落点 —— 分数高但不合规范，就不许用它。
    ctx_meta = dict(meta)
    ctx_meta.setdefault("chapter", chapter_title or "")
    ctx_meta.setdefault("section", section_title or "")
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    kind, confidence, rejected = "unclassified", 0.15, []
    for cand, sc in ordered:
        if sc <= 0 and cand != "lore":
            continue
        ok, probs = spec_mod.validate(cand, text, ctx_meta)
        if ok:
            kind = cand
            top = sc
            second = max([v for k2, v in scores.items() if k2 != cand] + [0.0])
            # lead = 领先幅度，归一到 [-1, 1]。
            # **分母必须取 |top| 与 |second| 的较大者**：早先写成 max(top, 1e-6)，
            # 于是在"高分候选被规范拒掉、只好采纳一个 0 分候选"时（top≈0, second=9），
            # lead = -9/1e-6 = -9e6，置信度算成 -4050000 —— 这批块在报告里被当成
            # "低置信"（203 个），接上模型后会被整批白白送去复核。
            lead = (top - second) / max(abs(top), abs(second), 1e-6)
            lead = max(-1.0, min(1.0, lead))
            confidence = round(max(0.05, min(0.98, 0.35 + 0.45 * lead + min(0.18, top / 40))), 3)
            if rejected:
                evidence.append(f"跳过不合规候选：{rejected[0]}")
            break
        rejected.append(f"{cand}({'；'.join(probs)})")

    subtype = discover_subtype(kind, title, text, section_title or chapter_title) if kind != "unclassified" else ""
    return Verdict(kind=kind, subtype=subtype, confidence=confidence, scores=scores, evidence=evidence)
