"""非游戏内容闸门（Gate）：**强约束，不是关键词过滤**。

背景：第一版把整本书都塞进知识层，于是版权页（"© 1981-2014 Chaosium Inc. 保留所有权利"）、
译者名单（Achronidas / lsr / 飙车致死法厄同…）、目录、更新历史全成了"世界观条目"。
DM 查"神话"时会撞到版权声明，检索排名还会被这些高频词带偏。

所以这里不做"过滤一下"，而是三条硬机制：

1. **单一写入口 + 默认拒绝**：知识层（block / entity / fact / 索引）只能经
   `pipeline._write_blocks` 与 `_run_extractors` 写入，两者都必须先过 `judge_*`。
   判不准的时候**倾向于拒**（fail-closed），并把"拒了什么、为什么"记进 `non_game_log`。
   宁可少一条世界观，也不要让版权声明混进 DM 的检索结果。
2. **判定要有出处**：每条判定返回 `code`（copyright / credits / toc / front_matter / url），
   不是布尔值。报告里能逐页复盘"这一页为什么被拒"，人工复核只看这张表就够。
3. **事后断言**：编译结束跑 `audit()`，在知识层里反查版权/网址/名单特征。
   命中数必须为 0；不为 0 就在报告里标红（约束要可验证，不能只靠口头约定）。

原文层与知识层的关系（这是第二个诉求）：

- **游戏内容**：原文层（`source_page`，逐页原样文本）+ 知识层（块/实体/事实）；
- **非游戏内容**：两层都不进，只在 `non_game_log` 留一条"页码 + 理由 + 内容指纹"。
  这样"非游戏内容不进本地存储"字面上成立，同时还能证明"这页我见过、判过、拒了"。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 结构层：整节拒绝

# 这些是"书的零件"，靠目录/书签就能定位，不需要看内容 —— 最硬的一类证据。
SECTION_DENY: list[tuple[str, str]] = [
    (r"^目录$", "toc"),
    (r"^使用说明$", "front_matter"),
    (r"^前言$", "front_matter"),
    (r"^(版权|版权页|版权信息)$", "copyright"),
    (r"^(译者|翻译|校对|致谢|鸣谢|参与人员)", "credits"),
    (r"^(更新历史|中文化进度表|版本历史)", "front_matter"),
    (r"^译名表", "front_matter"),
]

# ---------------------------------------------------------------- 内容层：文本规则

_RE_COPYRIGHT = re.compile(
    r"(©|\(c\)\s*\d{4}|Copyright|保留所有权利|版权所有|注册商标|®|"
    r"禁止[^\n]{0,8}(商业|转载|传播)|翻印必究|ISBN|定价[:：]|出版发行|印张|开本)",
    re.I,
)
_RE_URL = re.compile(r"(https?://|www\.[a-z0-9-]|@[a-z0-9-]+\.[a-z]{2,}|QQ\s*群|群号|帖子|论坛|网盘)", re.I)
# 名单行：2-10 字、没有标点、不像句子（译者/校对/测试人员名单就长这样）
_RE_NAME_LINE = re.compile(r"^[\u4e00-\u9fa5A-Za-z·．\.\- ]{2,12}$")
_NAME_STOP = {"目录", "前言", "正文", "附录", "规则", "技能", "战斗", "理智", "调查员", "守秘人"}

# 版权页/制作名单的骨架：一行一个"职务：人名"。一本书的 colophon 页就是十几行这个。
# 它比"纯人名行"更硬 —— 因为这些人名后面常跟着逗号和多个名字，纯人名规则抓不到。
# 一行里堆了多个人名（"Sandy Petersen Lynn Willis Paul Fricker and Mike Mason"）：
# 这是名单行，不是规则。中文人名/英文名都算，靠"连续 3 个首字母大写的词"或"3 个·分隔的名"。
_RE_NAMES_INLINE = re.compile(r"\b[A-Z][a-z]{2,}\b|[\u4e00-\u9fa5]{2,4}·[\u4e00-\u9fa5]{2,4}")
_RE_ROLE_LINE = re.compile(
    r"^(初版|修订|编辑|版面|艺术|封面|章首|插图|绘制|翻译|译者|校对|审校|排版|设计|出品|制作|监制|作者|美术|统筹)"
    r"[^：:\n]{0,8}[：:]"
)


@dataclass
class Verdict:
    keep: bool
    code: str = "ok"
    detail: str = ""
    score: float = 0.0

    def why(self) -> str:
        return f"{self.code}：{self.detail}" if self.detail else self.code


def judge_section(title: str) -> Verdict:
    """按标题整节拒绝。返回 keep=True 表示"不能凭标题定罪"，交给内容层再判。"""
    t = (title or "").strip()
    for pat, code in SECTION_DENY:
        if re.search(pat, t):
            return Verdict(False, code, f"标题命中「{t}」")
    return Verdict(True, "ok", "")


def _name_line_ratio(lines: list[str]) -> tuple[float, int]:
    """名单行占比：(比例, 名单行数)。用于识别译者/校对页。"""
    cand = 0
    total = 0
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        total += 1
        if _RE_NAME_LINE.match(t) and t not in _NAME_STOP and not re.search(r"[，。！？：:]", t):
            cand += 1
    return (cand / total if total else 0.0), cand


def judge_page(page) -> Verdict:
    """页级判定：版权页、名单页、链接页。

    两个必须的谨慎：
    - **短页才判版权**：正文页偶尔提一句"注册商标"不该整页毙掉；
    - **有表格的页不判名单**：译者名单是"一列纯人名"，而典籍表/载具表/法术表也是
      "一列短词"，实测把 p216（神话典籍）、p130（载具表）全误杀了。表格 = 内容。
    """
    page_no = getattr(page, "no", 0)
    t = getattr(page, "reading_text", lambda: "")() if page is not None else ""
    if len(t) < 20:
        return Verdict(False, "blank", f"p{page_no} 近乎空白", 1.0)

    hits = _RE_COPYRIGHT.findall(t)
    if hits and len(t) < 700:
        return Verdict(False, "copyright", f"p{page_no} 命中版权特征 {len(hits)} 处（{hits[0]}）", 0.95)

    urls = _RE_URL.findall(t)
    # 网址本身不是罪名：正规正文里也会让你"去 www.xxx 看看"（实测 p166 讲怎么找团，
    # 1600 字里两个网址，整页毙掉就丢了一整页真内容）。只有"整页就是一堆链接"才算。
    if len(urls) >= 2 and len(t) < 600:
        return Verdict(False, "url", f"p{page_no} 命中网址/社群特征 {len(urls)} 处且页面很短", 0.8)

    # 制作名单页（colophon）：≥2 行"职务：人名" 就是它 —— 这一条专治版权页长得像正文
    role_lines = [ln for ln in t.split("\n") if _RE_ROLE_LINE.match(ln.strip())]
    if len(role_lines) >= 2:
        return Verdict(False, "copyright", f"p{page_no} 制作名单行 {len(role_lines)} 行（如「{role_lines[0][:16]}」）", 0.92)

    try:
        has_table = bool(page.tables())
    except Exception:  # noqa: BLE001
        has_table = False
    ratio, n = _name_line_ratio(t.split("\n"))
    if (not has_table) and len(t) < 900 and n >= 8 and ratio >= 0.6 and t.count("。") <= 1:
        return Verdict(False, "credits", f"p{page_no} 名单行 {n} 行（占 {ratio:.0%}，无表格无句读）", 0.85)

    return Verdict(True, "ok", "")


def judge_block(text: str, title: str = "") -> Verdict:
    """块级判定：长页里混排的一小段版权也要挡住。

    名单行**不在这里判** —— 块级看不到"这一页是不是表格"，会误杀典籍表。
    名单只由页级判（有表格的页直接放过）。
    """
    t = (text or "").strip()
    if not t:
        return Verdict(False, "blank", "", 1.0)
    if len(t) < 500 and _RE_COPYRIGHT.search(t):
        return Verdict(False, "copyright", f"块内含版权特征：{t[:40]}", 0.9)
    if len(t) < 300 and len(_RE_URL.findall(t)) >= 2:
        return Verdict(False, "url", f"块内链接堆砌：{t[:40]}", 0.8)
    return Verdict(True, "ok", "")


def fingerprint(text: str) -> str:
    """非游戏内容的指纹：留着证明"见过"，但不留正文。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 行级清洗
#
# 整页拒绝太粗：这本书的译者名单是印在**第十章章首页**上的一行（"译/飙车致死法厄同 街猫口苗…"），
# 那一页其余全是正文。所以还要有一层"行级擦除"：只删那一行，别把整页毙掉。

_CREDIT_LINE = re.compile(r"^(译|翻译|校对|审校|排版|统筹)\s*[/／:：]")
_CREDIT_INLINE = re.compile(r"^译[/／]\s*\S+(\s+\S+){2,}$")
_COPY_LINE = re.compile(r"(©|\(c\)\s*\d{4}|保留所有权利|注册商标|禁止[^\n]{0,8}(商业|转载)|仅供学习|请支持正版|ISBN)", re.I)
# 译者注/校对说明：也是"非游戏内容"，但常夹在正文行中间（"该症状表由Achronidas 翻译。"）
_TRANSLATOR_NOTE = re.compile(r"(由\s*\S{1,20}\s*(翻译|整理|校对|录入)|译者注|译注|按原文翻|原文理解有误|感谢.{0,10}(翻译|校对))")
# 行内注记：整行是规则、只有括号里是译者话（"消耗：10+1D6 魔法值（译注：似为可变）"）——
# 这种情况**只删括号**，不能删整行，否则规则就没了
_INLINE_NOTE = re.compile(r"[（(]\s*(译注|译者注)\s*[:：][^）)]{0,120}[）)]")


def scrub_text(text: str) -> tuple[str, list[str]]:
    """行级擦除：去掉署名行、版权行。返回 (干净文本, 被删的行)。

    只删"整行就是这些东西"的行；正文里提到书名/出版社的句子不动。
    """
    # 段内嵌的署名句（"...该症状表由Achronidas 翻译。"）会被段落合并吃掉，行级规则抓不到，
    # 所以先在整段上删掉这类"谁翻译的"句子，再逐行清洗
    text = re.sub(
        r"[^。；\n]{0,10}由\s*[\u4e00-\u9fa5A-Za-z·]{1,20}\s*(?:翻译|校对|整理|录入)[。；]?", "", text or ""
    )
    keep, removed = [], []
    for ln in (text or "").split("\n"):
        t = ln.strip()
        if not t:
            continue
        # 译者注常直接接在句末、不带括号（"…都能看见他。译注：七版的潜行合并了…"）：
        # 从"译注"处截断，保留前面的规则正文
        m = re.search(r"\s*[（(]?\s*译注\s*[:：，]", ln)
        if m and m.start() >= 6:
            prefix = re.sub(r"[（(]\s*$", "", ln[: m.start()]).rstrip()
            removed.append(ln[m.start():].strip())
            if len(prefix) >= 6:
                keep.append(prefix)
            continue
        t2 = _INLINE_NOTE.sub("", ln).rstrip()
        if t2 != ln:
            removed.append(ln.strip())          # 注记本身进台账
            if len(t2.strip()) >= 6:
                keep.append(t2)                 # 剩下的规则正文留下
            continue
        if (
            _CREDIT_LINE.match(t)
            or _CREDIT_INLINE.match(t)
            or (len(t) < 80 and _TRANSLATOR_NOTE.search(t))  # 注记都很短，长行是正文
            or _RE_ROLE_LINE.match(t)                        # "编辑：xxx" 这类制作名单行
            # 注意：这里**不要**加"一行里 3+ 个人名"的规则 —— 实测把典籍表、
            # 洛夫克拉夫特书信引文全删了（597 行 vs 166 行），损失远大于收益。
            # 制作名单已由 _RE_ROLE_LINE（职务：人名）覆盖。
            or (len(t) < 120 and _COPY_LINE.search(t))
        ):
            removed.append(t)
            continue
        keep.append(ln)
    return "\n".join(keep), removed


# ---------------------------------------------------------------- 事后断言

# 审计必须比"页面判定"更严：页面判定里 `开本/印张/定价` 能帮我们认出 colophon，
# 但正文里"九卷对开本"是典籍的正常描述 —— 用它做断言会天天误报。
_AUDIT_COPYRIGHT = re.compile(
    r"(©|保留所有权利|版权所有|注册商标|禁止[^\n]{0,8}(商业|转载)|ISBN|Chaosium\s*Inc)", re.I
)
_AUDIT_RULES = [("copyright", _AUDIT_COPYRIGHT)]


def _audit_url(v: str) -> str:
    """网址只在"短值 + 多个链接"时才算污染 —— 正文里提一句官网是正常内容。"""
    hits = re.findall(r"(https?://|wwww?\.[a-z0-9-]|QQ\s*群|群号)", v or "", re.I)
    return hits[0] if (hits and (len(v) < 120 or len(hits) >= 2)) else ""


def audit(con) -> list[dict]:
    """在知识层里反查违规。**返回空列表才是合格编译**。

    只查"高置信特征"（版权、网址），不查名单行 —— 名单行在正常正文里也有（"力量/敏捷/意志"）。
    """
    bad: list[dict] = []
    for table, col, label in (
        ("block", "text", "block.text"),
        ("entity", "name", "entity.name"),
        ("fact", "raw", "fact.raw"),
    ):
        for r in con.execute(f"SELECT rowid, {col} v FROM {table} WHERE {col} IS NOT NULL").fetchall():
            v = r["v"] or ""
            if len(v) > 500:  # 长正文里的引用不算污染
                continue
            for code, rx in _AUDIT_RULES:
                m = rx.search(v)
                if m:
                    bad.append({"where": label, "code": code, "hit": m.group(0), "text": v[:60]})
                    break
            else:
                hit = _audit_url(v)
                if hit:
                    bad.append({"where": label, "code": "url", "hit": hit, "text": v[:60]})
    return bad
