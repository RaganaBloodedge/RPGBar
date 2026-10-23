"""LLM 判别器：**语义判断交给模型，真伪校验留给我们。**

为什么要有这一层，而不是在 `classify.py` 里塞个 `if use_llm`：

判别有两种做法，混在一起写必然变成四不像：

| 做法 | 谁来判 | 结果 |
|---|---|---|
| 规则判别（旧路） | 正则 + 词频 + 章节先验 | 对调过参的那本书准，换书就漏；**这是硬编码** |
| LLM 判别（新路） | 模型读文判断 | 换书也能判，但会编——必须有人验 |

所以分开：**模型负责"是什么"，这层负责"你说的能不能被原文证明"。**
模型给出 `kind` 和 `evidence`，这里逐字回查原文；对不上就判废，不给它圆场的机会。

三种判别任务，共用同一个骨架：

- `classify`：这块属于五类里的哪一类（+ 子类 + 证据）
- `extract`：这块里有哪些实体、各有哪些字段（+ 每个字段的原文引用）
- `split`：这块里到底有一条还是多条，切点在哪一行

三者都是"模型判 + 原文验"。区别只在契约（JSON Schema）与校验函数。
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .. import spec as spec_mod

# ------------------------------------------------------------------ 契约


@dataclass
class Verdict:
    """一条判别结果。`ok=False` 时 `why` 说明为什么没通过校验。"""

    key: str
    ok: bool
    value: dict = field(default_factory=dict)
    why: list[str] = field(default_factory=list)


# 输出越短越好：本地 4B 的 decode 只有 ~20 tok/s，每个多余字段都要按 token 付钱。
# 只留"类 + 短证据"，子类与置信度改用规则算（它们对判别的价值远低于这两个）。
CLASSIFY_SCHEMA = """{"items":[{"id":"<编号>","kind":"rule|pc|tool|npc|lore|unclassified","evidence":"<逐字摘自原文的 6-12 字>","canonical":<true|false>}]}"""

EXTRACT_SCHEMA = """{"items":[{"id":"<编号>","entities":[{"name":"<实体名，原文里的写法>",
"type":"item|skill|spell|monster|npc|rule|lore|attribute","subtype":"<2-6字>",
"fields":[{"label":"<字段名，2-6字>","value":"<值>","quote":"<逐字摘自原文的片段>"}]}]}]}"""

NAMES_SCHEMA = """{"items":[{"id":"<编号>","entities":[{"name":"<原文里的名字>","type":"item|skill|spell|monster|npc|rule|lore|attribute"}]}]}"""

FIELDS_SCHEMA = """{"items":[{"id":"<编号>","fields":[{"name":"<实体名，原文里的写法>","label":"<字段名 2-6 字>","value":"<值>","quote":"<逐字摘自原文>"}]}]}"""

SPLIT_SCHEMA = """{"items":[{"id":"<编号>","count":<这一条里有几个独立条目>,
"cuts":[{"after_line":<在第几行之后切，从1数>,"title":"<下一块从哪个标题开始>"}]}]}"""


# ------------------------------------------------------------------ 提示词

_COMMON_RULES = """你是一个跑团规则书的编译员。你在做**判别**，不是复述。

铁律：
1. **evidence / quote 必须逐字摘自给定原文**，一个字都不能改、不能概括。抄错会被程序判废。
2. 判断不了的填 `unclassified`（或 `count:1`），**不要猜**。猜错比留空更糟。
3. 只输出 JSON，不要解释、不要 markdown 代码块以外的任何文字。
"""

_CLASSIFY_BODY = """## 五类的边界（只能选这些）

- `rule` 规则类：一条规则 / 一个判定点（检定、掷骰、流程、限制）。含判定条件或数值。
- `pc` 玩家属性类：只描述**角色自身**的东西（属性值、技能点、职业、角色卡）。
- `tool` 工具类：物品、装备、武器、服务，**以名字为索引**。价格/射程/伤害等是它的字段。
- `npc` NPC 类：怪物、神话生物、有名有姓的人。通常是"名字 + 属性块 + 特殊能力"。
- `lore` 常识与世界观类：设定、历史、神话、地点、组织。叙述性文字，不构成判定。

容易混的两处：
- 讲"怎么算伤害"是 `rule`；讲"这把枪伤害多少"是 `tool`。
- 讲"理智值是什么"是 `pc`；讲"看到怪物掉多少理智"是 `rule`；讲"某个邪神的传说"是 `lore`。

**章节位置是强线索，优先级高于个别关键词**：
- 在"创建调查员""第三步/第五步/决定属性/决定职业/决定装备"里 → 基本是 `pc`，
  哪怕出现"伤害加值""技能点"这些像规则的字眼（那是角色的派生数值）；
- 在"战斗/追逐/理智/魔法/游戏系统"章里 → 基本是 `rule`；
- 在"介绍/爱手艺/克苏鲁神话/模组"章里的叙述段落 → `lore`。

## canonical：这段是不是"正典规则"
和类别正交的另一个判断，填 `true` 或 `false`：

- `false`：示例 / 演示 / 跑团实录 / 玩法举例 / 作者注 / 变更说明 / 勘误吐槽 ——
  **只是用来解释规则的示范文字**，本身不构成可执行的规则或可查的设定；
- `true`：其余一切（真规则、真数据、真设定）。

判据：把这段拿去回答"玩家现在该怎么做"时，它能不能直接作为依据？
"例如哈维想爬墙，守秘人让他掷攀爬"是 `false`；"攀爬失败可以孤注一掷"是 `true`。
拿不准就填 `true`（宁可多留，下面还有人工复核）。

## 待判条目
{items}"""

_EXTRACT_BODY = """## 要抽什么

从每条原文里抽出**实体**（能被 DM 按名字查到的东西）和它的**字段**（属性、数值、效果）。

- `type` 只能是 item（物品）/ skill（技能）/ spell（法术）/ monster（怪物）/ npc（人物）/
  rule（规则）/ lore（设定）/ attribute（属性项）。
- `fields` 的 `label` 用原文里的叫法（"伤害""射程""理智损失""基础值"），**不要自己造字段名**。
- 一个字段一条，`quote` 必须逐字摘自该条原文。

**不要抽正文里的普通名词**（"调查员""守秘人""这一轮"都不是实体）。
一条原文里没有实体就返回空数组 —— 空是对的，不是失败。

## 待抽条目
{items}"""

_NAMES_BODY = """## 要抽什么

从每条原文里找出**能被 DM 按名字查到**的实体：人物、怪物、地点、组织、物品、法术、规则名、设定名。

- 人名/怪物名要照原文的写法（"哈维·沃尔特斯"、"深潜者"）；
- **不要抽普通名词**（"调查员""守秘人""这一轮""物品"都不是实体）；
- 每条最多 6 个；没有就返回空数组 —— 空是对的，不是失败。

## 待抽条目
{items}"""

_FIELDS_BODY = """## 要抽什么

下面每条原文已经给出了**实体名**。请抽这些实体的字段（属性、数值、效果、身份、能力）。

- `label` 用原文里的叫法（"护甲""理智损失""职业""所在地"），不要自己造字段名；
- `value` 简短（≤20 字）；
- `quote` **逐字摘自该条原文**（≥6 字），抄错这条字段作废；
- 原文里没有的字段不要编。

## 待抽条目
{items}"""

_SPLIT_BODY = """## 要判什么

每条原文是切分器切出来的一块。判断它**内部是否还包含多个独立条目**，以及该在哪一行切开。

- 多个商品 / 多只怪物 / 多条技能各自成条 → `count` > 1，并给出切点。
- 一段连续叙述、一条规则、一张完整表格 → `count` = 1，`cuts` 为空。
- 切点用 `after_line` 表示：在第几行之后切（行号从 1 数，空行不算）。
- `title` 填切开后新块的开头标题（一般是那一行的实体名），**逐字摘抄开头几个字**。

判断标准是"**DM 会不会单独查它**"。会单独查的，就该独立成块。

三种最难判、也最该切的情形（切错比不切更糟，所以只在确实看出来时切）：

1. **名字和正文挤在同一行**："拜亚基在那里有节奏地扑打着的，是一群…" —— 这一行以怪物名
   开头，名字后面直接接说明。切点放在这一行**之前**，`title` 填那个名字（"拜亚基"）。
2. **属性块另起一只怪**："…属性|平均|掷骰 STR 90 … 混种深潜者他是个瘦削的男人…" ——
   上下文已经换了一只怪，`title` 填新怪的名字（"混种深潜者"）。
3. **题词署名不是条目**："——H．P．洛夫克拉夫特，《魔宴》" 是上一段引文的出处，
   **不要**把它当条目头切出来。

反例（**不要切**）：`特殊能力` `攻击` `战斗方式` `护甲` `法术` `教团` `其他特性` 这些是
同一条目内部的小标题，切开会把一只怪拆成几段；`count` 填 1。

## 待判条目
{items}"""


def build_prompt(task: str, items: list[tuple[str, str, str, str]], spec_text: str | None = None) -> str:
    """组批量提示词。`items` 是 [(id, 标题, 正文, 章节路径)]。

    批量而不是逐条：本地 4B 单条调用要 1-2 秒，2000 块逐条跑要一小时；
    一次 8 条能把总调用量压到 1/8，且同一批内上下文相邻，判断更一致。
    """
    body = {"classify": _CLASSIFY_BODY, "extract": _EXTRACT_BODY, "split": _SPLIT_BODY,
            "names": _NAMES_BODY, "fields": _FIELDS_BODY}[task]
    schema = {"classify": CLASSIFY_SCHEMA, "extract": EXTRACT_SCHEMA, "split": SPLIT_SCHEMA,
              "names": NAMES_SCHEMA, "fields": FIELDS_SCHEMA}[task]
    rendered = []
    for i, (_bid, title, text, path) in enumerate(items, 1):
        # **章节路径必须给**：这是规则判别一直占优的真正原因（`title_prior` 会按
        # "创建调查员"章→pc 加 5 分先验）。早先只给标题和正文，4B 没有这个上下文，
        # 于是把所有"说明性文字"都往 rule 上归 —— 实测 18 个错误里 14 个是 pc/lore→rule。
        head = f"### [{i}] 标题：{title}"
        if path:
            head += f"\n所属章节：{path}"
        rendered.append(f"{head}\n```\n{text[:500]}\n```")
    # 只拼一次规范。早先把 `spec.spec_text()` 全文也拼进来，等于五类边界写两遍 ——
    # 实测 prompt 从 3600 token 降到 ~1800，单批耗时从 50s 降到 25s。
    _ = spec_text  # 保留参数以兼容旧调用，但不再使用
    return f"{_COMMON_RULES}\n{body.format(items=chr(10).join(rendered))}\n\n## 输出格式\n{schema}"


# ------------------------------------------------------------------ 校验


def _flat(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _verify_quote(quote: str, text: str) -> bool:
    """证据必须能在原文里逐字找到（去空白后包含）。

    这是整套护栏的支点：模型可以编，但编的话就对不上原文。
    **不做模糊匹配** —— 模糊匹配等于替它圆场。

    唯一的放宽是"标点/空白/OCR 噪声"：4B 抄长句时常把「，」写成「,」或漏掉空格，
    这类差异不代表它在编。去标点后再比一次，仍然要求**连续包含**——编造的证据在
    去标点后也凑不出连续子串，所以拦得住。实测 121 条争议里绝大多数是这种噪声，
    不是模型在编。
    """
    q, t = _flat(quote), _flat(text)
    if not q:
        return False
    if q in t:
        return True
    q2, t2 = _strip_punct(q), _strip_punct(t)
    return bool(q2) and len(q2) >= 4 and q2 in t2


def _strip_punct(s: str) -> str:
    import unicodedata

    return "".join(
        ch for ch in s
        if not unicodedata.category(ch).startswith(("P", "Z", "S", "C"))
    )


def check_classify(raw: dict, text: str, meta: dict | None = None, title: str = "") -> Verdict:
    """校验模型给出的分类。

    **这里只查两件事：类在不在枚举里、证据能不能在原文里逐字找到（外加禁忌项）。**
    不再要求"必须命中某类证据特征"——那条是给**规则判别**用的护栏，用来防止词频打分乱归；
    模型读完整个块之后说"这是世界观"，比"这段没有句号"更可信。用正例检查去否模型，
    等于规则又一次压过模型，测出来就是"LLM 判了 151 块、全被判废"。

    保留的三道闸：
    1. `kind` 必须在五类枚举内（或明确 `unclassified`）；
    2. `evidence` 必须逐字出现在原文（防编造，这是整套护栏的支点）；
    3. `structural_forbidden()` 结构矛盾项（如"工具类不得含属性块"）—— 这些是
       结构上不可能的错，不是"不够典型"。
    """
    bid = str(raw.get("id") or "")
    kind = str(raw.get("kind") or "").strip()
    why: list[str] = []
    if kind not in spec_mod.SPEC and kind != "unclassified":
        why.append(f"非法类别 {kind!r}")
        return Verdict(bid, False, {"kind": "unclassified"}, why)

    ev = str(raw.get("evidence") or "").strip()
    # 证据的合法来源 = 标题 + 正文。块标题就是原文里的一行，模型从标题取证很常见；
    # 早先只拿 text 校验，实测把「第一章 介绍」这类块全判废了（15/151，原因全是
    # "evidence 无法在原文中逐字找到"）。
    haystack = f"{title}\n{text}" if title else text
    if kind != "unclassified":
        if not _verify_quote(ev, haystack):
            why.append("evidence 无法在原文中逐字找到")
        else:
            bad = spec_mod.structural_forbidden(kind, text, meta or {})
            if bad:
                why.append(f"违反该类禁忌：{bad}")

    if why:
        # 争议池入口：**模型的原始意见不能丢**。被拒不等于模型错——可能是证据抄歪、
        # 也可能是校验太严。llm_kind / llm_evidence 原样带回，由 classify 节点归入
        # disputed.json 落盘给人工裁决，而不是静默回落规则层。
        return Verdict(bid, False,
                       {"kind": "unclassified", "llm_kind": kind, "llm_evidence": ev},
                       why)
    # canonical 与类别正交：`false` 表示这是示例/演示/作者注，不是正典。
    # 模型没回、或回了个看不懂的值 → 一律按 true（宁可多留，不误删正文）。
    canon_raw = raw.get("canonical", True)
    if isinstance(canon_raw, str):
        canonical = canon_raw.strip().lower() not in ("false", "0", "no", "否")
    else:
        canonical = bool(canon_raw)
    return Verdict(
        bid, True,
        {"kind": kind, "subtype": str(raw.get("subtype") or "")[:8],
         # 模型不再回置信度：LLM 给的数字本身也不可校准，统一按"用了模型判"记 0.8，
         # 真实可信度由"证据是否命中"承担（命中=可信，否则判废）。
         "confidence": 0.8,
         "evidence": ev,
         "canonical": canonical},
    )


def check_extract(raw: dict, text: str) -> Verdict:
    """校验实体抽取：名字必须出现在原文、字段引用必须命中、类型必须在枚举内。"""
    bid = str(raw.get("id") or "")
    allowed = {"item", "skill", "spell", "monster", "npc", "rule", "lore", "attribute"}
    ents, why = [], []
    for e in raw.get("entities") or []:
        name = str(e.get("name") or "").strip()
        etype = str(e.get("type") or "").strip()
        if not name or etype not in allowed:
            why.append(f"实体非法：{name!r}/{etype!r}")
            continue
        if not _verify_quote(name, text):
            why.append(f"实体名不在原文：{name!r}")
            continue
        fields = []
        for f in e.get("fields") or []:
            label = str(f.get("label") or "").strip()
            value = str(f.get("value") or "").strip()
            quote = str(f.get("quote") or "").strip()
            if not label or not value:
                continue
            if not _verify_quote(quote or value, text):
                why.append(f"字段引用不在原文：{name}.{label}")
                continue
            fields.append({"label": label[:12], "value": value[:200], "quote": quote[:200]})
        ents.append({"name": name[:40], "type": etype,
                     "subtype": str(e.get("subtype") or "")[:8], "fields": fields})
    return Verdict(bid, True, {"entities": ents}, why)


def check_split(raw: dict, text: str, n_lines: int) -> Verdict:
    """校验切分：行号必须在范围内、切点必须递增、标题必须出现在对应行。"""
    bid = str(raw.get("id") or "")
    lines = [x for x in (text or "").split("\n") if x.strip()]
    n = len(lines) or n_lines
    count = int(raw.get("count") or 1)
    cuts, why = [], []
    if count <= 1:
        return Verdict(bid, True, {"count": 1, "cuts": []})
    last = 0
    for c in raw.get("cuts") or []:
        after = int(c.get("after_line") or 0)
        if not (0 < after < n) or after <= last:
            why.append(f"切点越界或乱序：{after}")
            continue
        # 标题必须真的出现在切点后的那一行里（去空白后包含）。
        # 不验的话模型可以报一个不存在的行号+编个标题，切出来的块开头是错的。
        title = str(c.get("title") or "").strip()
        if title:
            next_line = lines[after] if after < len(lines) else ""
            if _flat(title) not in _flat(next_line):
                why.append(f"切点标题与下一行不符：{title!r}")
                continue
        last = after
        cuts.append({"after_line": after, "title": title[:30]})
    return Verdict(bid, len(cuts) >= 1, {"count": len(cuts) + 1, "cuts": cuts}, why)


# ------------------------------------------------------------------ 执行


class Judge:
    """批量调用模型并校验。**任何一条校验不过，就回落给规则层，不硬用。**"""

    def __init__(self, model=None, *, batch: int = 8, workers: int = 3, temperature: float = 0.0):
        if model is None:
            from .llm_classify import get_default_model

            model = get_default_model()
        self.model = model
        self.batch = batch
        self.workers = workers
        self.stats = {"calls": 0, "ok": 0, "rejected": 0, "items": 0}

    @property
    def available(self) -> bool:
        return self.model is not None

    # ---- 底层：一次批量调用 → 原始 items（**id 转回全局**）
    def _call(self, task: str, chunk: list[tuple[str, str, str, str]]) -> list[dict]:
        """`chunk` 是 [(全局id, 标题, 正文)]。

        提示词里用的是**批内局部编号**（1..len(chunk)，短、模型不容易抄错），
        返回时立刻换回全局 id。早先直接在全局 id 上做映射，结果每批的 1..8 都落到
        前 8 个块上 —— 表现为"判了 19 块、只采纳 8 条"，非常难查。

        **失败重试一次**（handoff 第 0 步第 4 条）：本地 llama-server 单槽，3 个并发
        打过去偶尔整批超时/回空 JSON。不重试的代价是这一批 8 个块**静默**回落规则层，
        实测 20 个批瞬时失败、报告里完全看不出来。只重试一次：真挂的端点重试十次也没用。
        """
        from .. import spec as spec_mod2

        local = [(str(i + 1), t, x, p) for i, (_g, t, x, p) in enumerate(chunk)]
        prompt = build_prompt(task, local, spec_mod2.spec_text() if task == "classify" else None)
        items: list[dict] | None = None
        # 输出预算按批大小给：4B 在"输出被截断"时表现为非法 JSON，而不是报错。
        # 截断就放大预算重问，原样重问只会得到同一段半截 JSON。
        budget = {
            "classify": max(320, 90 * len(chunk)),
            "extract": max(600, 260 * len(chunk)),
            "split": max(200, 110 * len(chunk)),
            "names": max(200, 120 * len(chunk)),
            "fields": max(300, 200 * len(chunk)),
        }.get(task, 800)
        for attempt in (1, 2, 3):
            self.stats["calls"] += 1
            try:
                m = self.model.bind(max_tokens=budget) if hasattr(self.model, "bind") else self.model
                msg = m.invoke(prompt)
            except Exception as exc:  # 网络/额度问题不该让整本书编译失败
                if attempt < 3:
                    print(f"[judge] {task} 批调用失败，重试：{type(exc).__name__}", file=sys.stderr)
                    continue
                self.stats["failed_calls"] = self.stats.get("failed_calls", 0) + 1
                print(f"[judge] {task} 批调用失败（{len(chunk)} 块无判定）", file=sys.stderr)
                return []
            raw = getattr(msg, "content", msg)
            if isinstance(raw, list):
                raw = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in raw)
            data = _extract_json(str(raw))
            got = data.get("items") if isinstance(data, dict) else None
            if isinstance(got, list):
                items = got
                break
            if attempt < 3:
                budget = min(int(budget * 1.6), 4000)
                print(f"[judge] {task} 输出不是合法 JSON，放大预算重试（{budget}）", file=sys.stderr)
        if items is None:
            self.stats["failed_calls"] = self.stats.get("failed_calls", 0) + 1
            print(f"[judge] {task} 输出不是合法 JSON（{len(chunk)} 块无判定）", file=sys.stderr)
            return []
        out: list[dict] = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            try:
                n = int(it.get("id") or 0)
            except (TypeError, ValueError):
                n = 0
            if not (1 <= n <= len(chunk)):  # 模型可能不回 id，或回了个不存在的
                n = i + 1
            if not (1 <= n <= len(chunk)):
                continue
            it["id"] = chunk[n - 1][0]  # 换回全局 id
            out.append(it)
        return out

    def _chunks(self, items: list[tuple[str, str, str, str]]):
        for i in range(0, len(items), self.batch):
            yield items[i : i + self.batch]

    # ---- 三种判别
    def classify(self, items: list[tuple[str, str, str, dict]]) -> dict[str, Verdict]:
        """items: [(block_id, title, text, meta)] → {block_id: Verdict}"""
        if not self.available or not items:
            return {}
        meta_of = {b: m for b, _t, _x, m in items}
        text_of = {b: x for b, _t, x, _m in items}
        title_of = {b: t for b, t, _x, _m in items}

        def _path(m: dict) -> str:
            ch, sec = (m or {}).get("chapter") or "", (m or {}).get("section") or ""
            return f"{ch} > {sec}".strip(" >")

        triples = [(b, t, x, _path(m)) for b, t, x, m in items]
        out: dict[str, Verdict] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for raws in pool.map(lambda c: self._call("classify", c), list(self._chunks(triples))):
                for raw in raws:
                    bid = str(raw.get("id"))
                    if bid not in text_of:
                        continue
                    v = check_classify(raw, text_of[bid], meta_of.get(bid) or {},
                                       title=title_of.get(bid, ""))
                    self.stats["items"] += 1
                    self.stats["ok" if v.ok else "rejected"] += 1
                    out[bid] = v
        return out

    def extract(self, items: list[tuple[str, str, str]]) -> dict[str, Verdict]:
        if not self.available or not items:
            return {}
        text_of = {b: x for b, _t, x in items}
        triples = [(b, t, x, "") for b, t, x in items]
        out: dict[str, Verdict] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for raws in pool.map(lambda c: self._call("extract", c), list(self._chunks(triples))):
                for raw in raws:
                    bid = str(raw.get("id"))
                    if bid not in text_of:
                        continue
                    out[bid] = check_extract(raw, text_of[bid])
                    self.stats["items"] += 1
        return out

    def names(self, items: list[tuple[str, str, str]]) -> dict[str, list[dict]]:
        """第一段：**只抽实体名**（输出短，4B 不会截断）。名字必须出现在原文里。"""
        if not self.available or not items:
            return {}
        text_of = {b: x for b, _t, x in items}
        triples = [(b, t, x, "") for b, t, x in items]
        out: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for raws in pool.map(lambda c: self._call("names", c), list(self._chunks(triples))):
                for raw in raws:
                    bid = str(raw.get("id"))
                    if bid not in text_of:
                        continue
                    got = []
                    for e in raw.get("entities") or []:
                        name = str(e.get("name") or "").strip()
                        etype = str(e.get("type") or "").strip()
                        if not name or etype not in (
                            "item", "skill", "spell", "monster", "npc", "rule", "lore", "attribute"
                        ):
                            continue
                        if not _verify_quote(name, text_of[bid]):
                            continue  # 名字不在原文 = 编的
                        got.append({"name": name[:40], "type": etype})
                    if got:
                        out[bid] = got[:6]
        return out

    def fields(self, items: list[tuple[str, str, str]]) -> dict[str, list[dict]]:
        """第二段：给定了实体名，抽字段。每条字段的 quote 必须在原文里逐字找到。"""
        if not self.available or not items:
            return {}
        text_of = {b: x for b, _t, x in items}
        triples = [(b, t, x, "") for b, t, x in items]
        out: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for raws in pool.map(lambda c: self._call("fields", c), list(self._chunks(triples))):
                for raw in raws:
                    bid = str(raw.get("id"))
                    if bid not in text_of:
                        continue
                    got = []
                    for f in raw.get("fields") or []:
                        name = str(f.get("name") or "").strip()
                        label = str(f.get("label") or "").strip()
                        value = str(f.get("value") or "").strip()
                        quote = str(f.get("quote") or "").strip() or value
                        if not (name and label and value):
                            continue
                        if not _verify_quote(name, text_of[bid]):
                            continue
                        if not _verify_quote(quote, text_of[bid]):
                            continue  # 编的引文直接丢
                        got.append({"name": name[:40], "label": label[:12],
                                    "value": value[:60], "quote": quote[:200]})
                    if got:
                        out[bid] = got[:20]
        return out

    def split(self, items: list[tuple[str, str, str]]) -> dict[str, Verdict]:
        if not self.available or not items:
            return {}
        text_of = {b: x for b, _t, x in items}
        triples = [(b, t, x, "") for b, t, x in items]
        out: dict[str, Verdict] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for raws in pool.map(lambda c: self._call("split", c), list(self._chunks(triples))):
                for raw in raws:
                    bid = str(raw.get("id"))
                    if bid not in text_of:
                        continue
                    out[bid] = check_split(raw, text_of[bid], len(text_of[bid].split("\n")))
        return out

    def is_demo(self, items: list[tuple[str, str, str]]) -> dict[str, bool]:
        """这段是不是"用具体人物把规则演一遍"的示例（canonical=false）？

        单独问、问得极短：4B 在"顺便判断一下"的复合任务里会一律回 true（实测：把
        「1.3 游戏范例」整段判成正典）。把它拆成一个二选一问题，它就能答对。
        只对"看起来像示例"的块调用（见 nodes._demo_suspect），一次几块，成本可忽略。
        """
        if not self.available or not items:
            return {}
        out: dict[str, bool] = {}
        for bid, title, text in items:
            prompt = (
                "判断下面这段文字是不是**示例**。\n\n"
                "示例 = 用具体人物把规则/流程演一遍：范例游戏、跑团实录（\"守秘人：… 凯西：…\"）、"
                "举例说明（\"例如，哈维想爬墙……\"）、作者注。它是教学材料，不是规则本身。\n"
                "不是示例 = 规则条文、数值表、设定描述、人物/怪物介绍。\n\n"
                f"标题：{title}\n```\n{text[:800]}\n```\n\n"
                '只输出 JSON：{"demo":true|false}'
            )
            try:
                m = self.model.bind(max_tokens=24)
                raw = getattr(m.invoke(prompt), "content", "")
            except Exception:  # noqa: BLE001
                continue
            data = _extract_json(str(raw))
            if isinstance(data, dict) and isinstance(data.get("demo"), bool):
                out[bid] = data["demo"]
        return out


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个完整 JSON 对象（允许被 ```json 包裹或前后带话）。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except Exception:
                    return None
    return None
