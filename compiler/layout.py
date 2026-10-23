"""★ 版式画像（Layout Profiling）：**"摸清版式"的统一范式**。

不同的文档是不同类型的东西：规则书、小说、技术手册、论文、扫描件。它们的结构信号完全
不同 —— 规则书靠"章 + 表 + 属性块"，小说靠"节拍 + 空行"，手册靠"1.2.3 编号"，扫描件
干脆没有文字层。如果把这些判断散落在抽取器里，每接一种新文档就要改一遍抽取器。

所以这一层只做一件事：**把"这本书该怎么读"变成一份可落盘、可打分、可解释的画像**，
下游（outline / classify / extract）全部只读画像，不自己猜。

四步范式（对任何文档都一样）：

```
1. 证据（probe）      书签？目录页？字号直方图？缩进直方图？表格网格？文字层质量？
2. 假设（hypotheses）  若干"标题识别家族"：CJK 章回 / 阿拉伯点分 / 拉丁 Chapter / 字号聚类 / 目录页反解
3. 打分（score）       有书签 ⇒ 用书签算召回（这是免费的标注集）；没书签 ⇒ 用"自洽性"打分
                      （同类命中行的字号是否一致、是否短行、前后是否有缩进/空行变化）
4. 定策（select）      选出主策略 + 后备链，连证据一起写进 layout_profile.json
```

关键点：**书签不是靠山，是校准器**。有书签就用它验证"我认标题认得准不准"，没有书签
也能跑，只是置信度低、并且在报告里明说。这样同一套代码既能吃 COC 这种带 176 条书签的
电子书，也能吃只有字号的扫描转文字版。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .reader import Book, Line, detect_tables, is_page_number

# ------------------------------------------------------------------ 标题家族（可扩展的规则库）
#
# 每个家族 = 一种"这本书怎么标标题"的假设。level 是它自称的层级，confidence 来自打分。
# 加新文档类型时通常只需要往这里加一个家族，而不是改下游代码。

@dataclass(frozen=True)
class HeadingFamily:
    key: str
    level: int
    pattern: str
    note: str
    max_len: int = 40

    def match(self, line: Line, body_size: float = 0.0) -> bool:
        if len(line.text) > self.max_len:
            return False
        if self.key == "font_cluster":
            # 字号家族没有正则：靠"明显大于正文 + 短行 + 不是句子结尾"三个条件
            return bool(
                body_size
                and line.size >= body_size + 1.0
                and line.is_short
                and not line.ends_sentence
            )
        return bool(re.match(self.pattern, line.text.strip()))


FAMILIES: list[HeadingFamily] = [
    # —— 中文书 ——
    HeadingFamily("cjk_chapter", 1, r"^第[〇零一二三四五六七八九十百千0-9]+[章篇部卷]", "第X章"),
    HeadingFamily("cjk_section", 2, r"^第[〇零一二三四五六七八九十百千0-9]+[节節]", "第X节"),
    HeadingFamily("cjk_enum", 2, r"^[一二三四五六七八九十]+[、.．]", "一、二、"),
    # —— 编号书（手册 / 规则书 / 教材）——
    HeadingFamily("arabic_dotted", 2, r"^\d{1,2}\.\d{1,2}(\.\d{1,2})?\s*[\u4e00-\u9fa5A-Za-z]", "1.2 / 1.2.3"),
    HeadingFamily("arabic_major", 1, r"^\d{1,2}\.\s+[\u4e00-\u9fa5A-Za-z]", "1. 标题"),
    # —— 英文书 ——
    HeadingFamily("latin_chapter", 1, r"^(Chapter|CHAPTER|Part|PART)\s+([0-9]+|[IVXLC]+)\b", "Chapter N"),
    HeadingFamily("latin_dotted", 2, r"^\d{1,2}\.\d{1,2}(\.\d{1,2})?\s+[A-Za-z]", "1.2 Title"),
    HeadingFamily("all_caps", 2, r"^[A-Z][A-Z0-9 ,'\-]{2,39}$", "全大写短行"),
    # —— 通用：表 / 图 / 附录（跨类型都在用）——
    HeadingFamily("table_caption", 3, r"^表\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ0-9IVX]+[:：]", "表Ⅰ："),
    HeadingFamily("figure_caption", 3, r"^(图|Figure|Fig\.)\s*[0-9IVX]+", "图1"),
    HeadingFamily("appendix", 2, r"^附录\s*[ⅠⅡⅢⅣⅤ0-9A-Z]*[:：]?", "附录Ⅰ："),
    HeadingFamily("part_marker", 2, r"^(Part|PART)\s*[0-9IVX]+[:：]", "Part 1:"),
    # —— 纯字号家族（兜底：没有编号、没有书签的书只能靠它）——
    HeadingFamily("font_cluster", 2, r"", "字号明显大于正文的短行"),
]
_FAMILY_BY_KEY = {f.key: f for f in FAMILIES}


# ------------------------------------------------------------------ 画像本体

@dataclass
class LayoutProfile:
    """一份"这本书该怎么读"的画像。所有字段都带得出处，报告里原样打印。"""

    book_title: str
    page_count: int
    doc_kind: str = "generic"          # rulebook / novel / manual / academic / scanned / generic
    doc_kind_evidence: list[str] = field(default_factory=list)

    # 文字层质量（决定要不要 OCR / 要不要告警）
    chars_per_page: float = 0.0
    empty_page_ratio: float = 0.0
    text_density: float = 0.0          # 有字行数 / 总行数

    # 版面
    page_sizes: list[list[float]] = field(default_factory=list)
    body_left: float = 0.0
    body_right: float = 0.0
    columns: int = 1

    # 字体
    body_size: float = 0.0
    size_histogram: dict = field(default_factory=dict)   # "10.6" -> 字数占比
    heading_sizes: list[float] = field(default_factory=list)
    bold_used: bool = False

    # 结构证据
    has_bookmarks: bool = False
    bookmark_levels: dict = field(default_factory=dict)
    bookmark_count: int = 0
    has_toc_page: bool = False
    toc_pages: list[int] = field(default_factory=list)
    toc_entry_count: int = 0

    # 表格
    table_count: int = 0
    table_pages: list[int] = field(default_factory=list)
    table_cols: dict = field(default_factory=dict)

    # 家族命中统计（校验用）: key -> {"hits": n, "recall": 0-1}
    family_scores: dict = field(default_factory=dict)
    enabled_families: list[str] = field(default_factory=list)
    primary_family: str = ""

    # 行文特征（给 classify 的先验）
    numbering_style: str = ""
    avg_line_chars: float = 0.0
    dialog_ratio: float = 0.0          # 含引号行占比 → 小说/模组高
    dice_ratio: float = 0.0            # 含骰子表达式行占比 → 规则书高

    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    page_offset: int | None = None  # PDF 页索引 - 书里印的页码（目录/引用用印刷页码）

    def families(self) -> list[HeadingFamily]:
        return [_FAMILY_BY_KEY[k] for k in self.enabled_families if k in _FAMILY_BY_KEY]

    def to_json(self) -> dict:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ------------------------------------------------------------------ 探针

_DICE_RE = re.compile(r"\b\d*[dD]\d+\b|[0-9]+\s*/\s*[0-9]+")
_QUOTE_RE = re.compile(r"[“”「」『』\"]")


def _norm_title(s: str) -> str:
    return re.sub(r"[\s\u3000:：.·、，,（）()【】\[\]…-]+", "", s or "")


def _similar(a: str, b: str) -> float:
    a, b = _norm_title(a), _norm_title(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def probe(book: Book, base: LayoutProfile | None = None) -> LayoutProfile:
    """第 1 步：只测量，不下结论。`base` 用来接住前面已经测到的页眉页脚带等结果。"""
    prof = base or LayoutProfile(book_title=book.title, page_count=book.page_count)
    prof.book_title = book.title
    prof.page_count = book.page_count
    if not book.pages:
        prof.warnings.append("PDF 没有可读页面")
        return prof

    # 页尺寸 / 左右边界 / 正文字号：都用众数，抗住个别插图页
    sizes = Counter((round(p.width), round(p.height)) for p in book.pages)
    prof.page_sizes = [list(k) for k, _ in sizes.most_common(3)]

    lefts, rights, body_sizes, all_sizes = Counter(), Counter(), Counter(), Counter()
    chars_total = 0
    empty = 0
    line_chars = []
    quote_lines = dice_lines = body_lines_total = 0

    for p in book.pages:
        bl = p.body_lines()
        text = (p.text() or "").strip()
        chars_total += len(text)
        if len(text) < 20:
            empty += 1
        if not bl:
            continue
        l0, r0 = p.margins()
        lefts[round(l0)] += 1
        rights[round(r0)] += 1
        for ln in bl:
            body_sizes[ln.size] += len(ln.text)
            line_chars.append(len(ln.text))
            body_lines_total += 1
            if _QUOTE_RE.search(ln.text):
                quote_lines += 1
            if _DICE_RE.search(ln.text):
                dice_lines += 1
        for ln in p.lines:
            all_sizes[ln.size] += len(ln.text)

    prof.chars_per_page = round(chars_total / max(1, book.page_count), 1)
    prof.empty_page_ratio = round(empty / book.page_count, 3)
    prof.body_left = float(lefts.most_common(1)[0][0]) if lefts else 0.0
    prof.body_right = float(rights.most_common(1)[0][0]) if rights else 0.0
    prof.body_size = float(body_sizes.most_common(1)[0][0]) if body_sizes else 0.0
    total_chars = sum(all_sizes.values()) or 1
    prof.size_histogram = {
        f"{sz}": round(n / total_chars, 4) for sz, n in all_sizes.most_common(6)
    }
    # 标题字号 = 明显大于正文且成规模的字号（占比 >0.15% 才不是个别花字）
    prof.heading_sizes = sorted(
        {sz for sz, n in all_sizes.items() if sz >= prof.body_size + 1.0 and n / total_chars > 0.0015},
        reverse=True,
    )
    prof.avg_line_chars = round(sum(line_chars) / max(1, len(line_chars)), 1)
    prof.dialog_ratio = round(quote_lines / max(1, body_lines_total), 3)
    prof.dice_ratio = round(dice_lines / max(1, body_lines_total), 3)

    # 双栏检测：右边界不统一 → 说明一页里有两个文本列（这里用"行右端落在两个簇"近似）
    right_vals = sorted(rights)
    prof.columns = 1
    if len(right_vals) >= 2 and (right_vals[-1] - right_vals[0]) > 80:
        # 只有两簇都占相当比例才算双栏，否则只是个别短行
        top2 = rights.most_common(2)
        if len(top2) >= 2 and top2[1][1] >= book.page_count * 0.25:
            prof.columns = 2

    # 书签 / 目录页
    prof.has_bookmarks = bool(book.bookmarks)
    prof.bookmark_count = len(book.bookmarks)
    prof.bookmark_levels = dict(Counter(lv for lv, _, _ in book.bookmarks))
    toc_pages, toc_entries = _probe_toc_pages(book)
    prof.has_toc_page = bool(toc_pages)
    prof.toc_pages = toc_pages
    prof.toc_entry_count = toc_entries

    # 表格
    table_pages, cols = [], Counter()
    for p in book.pages:
        tbs = detect_tables(p)
        if tbs:
            table_pages.append(p.no)
            for t in tbs:
                cols[len(t.header)] += 1
    prof.table_count = sum(cols.values())
    prof.table_pages = table_pages
    prof.table_cols = dict(cols)

    # 文字层质量
    prof.text_density = round(
        sum(len(p.body_lines()) for p in book.pages) / max(1, sum(len(p.lines) for p in book.pages)), 3
    )
    return prof


def _probe_toc_pages(book: Book) -> tuple[list[int], int]:
    """找"目录页"：既认标题词，也认版式（大量「标题 ...... 页码」的行）。"""
    pages: list[int] = []
    entries = 0
    # 注意 re.M：目录是一行一条，"$"必须允许落在换行前，否则整页只会匹配到最后一行
    leader = re.compile(
        r"[.．·…]{3,}\s*\d{1,4}\s*$|[\u4e00-\u9fa5）)】]\s+\d{1,4}\s*$", re.M
    )
    for p in book.pages[:40]:  # 目录总在书前部
        txt = p.text()
        hits = len(leader.findall(txt))
        if hits >= 5 or ("目录" in txt[:80] and hits >= 3) or ("Contents" in txt[:80] and hits >= 3):
            if hits >= 3:
                pages.append(p.no)
                entries += hits
    return pages, entries


# ------------------------------------------------------------------ 第 2-3 步：假设与打分

def _candidate_lines(book: Book, sample_pages: list[int] | None = None) -> list[Line]:
    pages = book.pages if sample_pages is None else [book.pages[i - 1] for i in sample_pages if 0 < i <= len(book.pages)]
    out = []
    for p in pages:
        for ln in p.body_lines():
            if is_page_number(ln.text):
                continue
            out.append(ln)
    return out


def _detect_bands_prelude(book: Book) -> LayoutProfile:
    """先建一个空画像，用来承载页眉页脚探测的结论（probe 之后会往同一个对象里填）。"""
    return LayoutProfile(book_title=book.title, page_count=book.page_count)


def _detect_page_offset(book: Book) -> int | None:
    """PDF 页序 ↔ 书里印刷页码的偏移。

    规则书/出版物几乎都是「PDF 第 9 页 = 印刷第 1 页」，而目录、正文里的"见第 143 页"
    用的全是印刷页码。算出来存进画像，后面 目录条目 / 交叉引用 才能落到正确的页。
    """
    diffs: list[int] = []
    for p in book.pages:
        for ln in p.lines:
            if ln.y >= p.height * 0.88 and is_page_number(ln.text) and ln.text.strip().isdigit():
                diffs.append(p.no - int(ln.text.strip()))
                break
    if not diffs:
        return None
    # 前后封面/插图页常常不印页码，出现次数最多的那个偏移才是正文的偏移
    return Counter(diffs).most_common(1)[0][0]


def _detect_bands(book: Book, prof: LayoutProfile) -> tuple[float, float]:
    """测页眉/页脚带：**不按"页面高度 7%"这种硬比例**，而是按"哪一行在重复"。

    做法：只看靠近上下边缘的行，统计同一串文字出现在多少个不同页面上。同一串文字在
    20+ 页的同一位置出现，那它就是页眉/页脚（章节名、书名、页码），不是正文。
    这样 A4/16 开/扫描件的页边距差别再大也吃得下。
    """
    if not book.pages:
        return 0.0, 1e9
    h = max(p.height for p in book.pages)
    top_zone = h * 0.16
    bottom_zone = h * 0.88
    top_texts: dict[str, set[int]] = {}
    bottom_texts: dict[str, set[int]] = {}
    top_y: dict[str, float] = {}
    bottom_y: dict[str, float] = {}

    for p in book.pages:
        for ln in p.lines:
            if is_page_number(ln.text):
                continue
            key = re.sub(r"\s+", "", ln.text)
            if len(key) > 40:
                continue
            if ln.y <= top_zone:
                top_texts.setdefault(key, set()).add(p.no)
                top_y.setdefault(key, ln.y)
            elif ln.y >= bottom_zone:
                bottom_texts.setdefault(key, set()).add(p.no)
                bottom_y.setdefault(key, ln.y)

    need = max(3, int(book.page_count * 0.06))
    top_band, bottom_band = 0.0, h * 1.2
    repeated_top = sorted((y for k, y in top_y.items() if len(top_texts[k]) >= need), reverse=True)
    repeated_bottom = sorted(y for k, y in bottom_y.items() if len(bottom_texts[k]) >= need)

    if repeated_top:
        top_band = repeated_top[0] + 6  # 让页眉行本身落在带外
        prof.notes.append(f"页眉带 {top_band:.0f}pt：{_sample(top_texts, top_y, top_band, True)}")
    if repeated_bottom:
        bottom_band = repeated_bottom[0] - 6
        prof.notes.append(f"页脚带 {bottom_band:.0f}pt：{_sample(bottom_texts, bottom_y, bottom_band, False)}")
    return top_band, bottom_band


def _sample(table: dict[str, set[int]], ys: dict[str, float], band: float, top: bool) -> str:
    items = [(k, len(v)) for k, v in table.items() if (ys[k] <= band if top else ys[k] >= band)]
    items.sort(key=lambda kv: -kv[1])
    return "、".join(f"{k}({n}页)" for k, n in items[:3])


def _score_family(book: Book, fam: HeadingFamily, lines: list[Line], body_size: float) -> dict:
    """给一个标题家族打分。

    - 有书签：召回率 = 书签里有几条能在"它标注的那一页"上被这个家族认出来。
      这是最硬的证据 —— 书签就是作者亲手标的标题清单。
    - 没书签：自洽性 = 命中行里"短行 + 不是句子结尾"的比例，再乘"命中密度合理"的系数
      （一页命中上百行说明这个正则在匹配正文，属于噪声）。
    - 两者都乘 `min(1, hits/8)`：只命中 1 次的家族不该拿高分（否则一个巧合就能当主策略）。
    """
    hits = [ln for ln in lines if fam.match(ln, body_size)]
    info = {"hits": len(hits), "recall": 0.0, "consistency": 0.0, "noise": 0.0}

    if book.bookmarks:
        by_page: dict[int, list[str]] = {}
        for _lv, title, pg in book.bookmarks:
            by_page.setdefault(pg, []).append(title)
        got = 0
        for pg, titles in by_page.items():
            cands = [ln.text for ln in in_lines(book, pg) if fam.match(ln, body_size)]
            for t in titles:
                if any(_similar(t, c) >= 0.62 for c in cands):
                    got += 1
        info["recall"] = round(got / max(1, sum(len(v) for v in by_page.values())), 3)

    if hits:
        good = sum(1 for ln in hits if ln.is_short and not ln.ends_sentence)
        info["consistency"] = round(good / len(hits), 3)
        per_page = len(hits) / max(1, len({ln.page for ln in hits}))
        info["noise"] = round(min(1.0, per_page / 6.0), 3)

    base = max(info["recall"], info["consistency"] * (1 - info["noise"]) * 0.85)
    info["score"] = round(base * min(1.0, len(hits) / 8.0), 3)
    return info


def in_lines(book: Book, page_no: int) -> list[Line]:
    if 1 <= page_no <= len(book.pages):
        return book.pages[page_no - 1].body_lines()
    return []


# ------------------------------------------------------------------ 第 4 步：定策

def _guess_kind(book: Book, prof: LayoutProfile) -> None:
    """文档类型判定。**它只影响先验和告警，不影响能不能跑通** —— 这是刻意的。"""
    if prof.chars_per_page < 120 and prof.empty_page_ratio > 0.5:
        prof.doc_kind = "scanned"
        prof.doc_kind_evidence.append(f"每页仅 {prof.chars_per_page} 字、{prof.empty_page_ratio:.0%} 页近乎空白 → 疑似扫描件（无文字层）")
        prof.warnings.append("文字层几乎为空：需要 OCR 才能编译，当前只能做版式保守估计")
        return

    sample = "\n".join(p.text() for p in book.pages[: min(len(book.pages), 60)])
    rule_kw = {"检定": 3, "掷骰": 3, "调查员": 2, "守秘人": 2, "d100": 2, "属性": 1, "技能": 1}
    scores = {
        "rulebook": sum(sample.count(k) * w for k, w in rule_kw.items()),
        "novel": sum(sample.count(k) for k in ("他说", "她说", "“")) + prof.dialog_ratio * 500,
        "manual": len(re.findall(r"^\d+\.\d+(\.\d+)?\s+\S", sample, re.M)) * 3,
    }
    # 表格多 + 骰子多 + 结构词多 → 规则书
    if prof.table_count >= 3:
        scores["rulebook"] += 8
    if prof.dice_ratio > 0.02:
        scores["rulebook"] += 10
    if prof.chars_per_page > 900:
        scores["novel"] += 5
    kind = max(scores, key=scores.get)
    if scores[kind] < 12:
        kind = "generic"
    prof.doc_kind = kind
    prof.doc_kind_evidence.append(
        "类型打分 " + "，".join(f"{k}={v:.0f}" for k, v in scores.items()) + f" → {kind}"
    )


def _header_texts(book: Book) -> frozenset:
    """顶部居中、且在 **≥3 页** 重复的行 = 页眉（含"节名型页眉"）。

    为什么需要第二套判据：`_detect_bands` 要求"同一串文字在全书 6% 以上的页重复"，
    这对章名很合适，但对**节名**太严 —— 附录Ⅲ 只有 7 页，它顶部那行「附录Ⅲ：物价表」
    永远够不上阈值，于是被当成正文缝进块里（实测 p381 的商品清单里混进了这一行）。

    这里把阈值降到 3 页，但加上"水平居中"这个前提。正文不会居中，所以误杀风险很低；
    而"居中 + 顶部 + 跨页重复"正是页眉的定义。
    """
    if not book.pages:
        return frozenset()
    h = max(p.height for p in book.pages)
    top_zone = h * 0.16
    seen: dict[str, set[int]] = {}
    for p in book.pages:
        for ln in p.lines:
            if ln.y > top_zone or is_page_number(ln.text):
                continue
            if abs((ln.x0 + ln.x1) / 2 - p.width / 2) >= 30:
                continue
            key = re.sub(r"\s+", "", ln.text)
            if 2 <= len(key) <= 40:
                seen.setdefault(key, set()).add(p.no)
    return frozenset(k for k, pages in seen.items() if len(pages) >= 2)


def profile(book: Book, save_to: str | Path | None = None) -> LayoutProfile:
    """完整走一遍四步范式，返回可落盘的画像。"""
    # 0) 先把页眉页脚带测出来并回填 —— 后面所有"正文行"统计都依赖它
    prof = _detect_bands_prelude(book)
    top, bottom = _detect_bands(book, prof)
    from . import reader as _reader

    headers = _header_texts(book)
    _reader.set_bands(book, top, bottom, headers)
    if headers:
        prof.notes.append(f"页眉文字黑名单 {len(headers)} 条（示例：{'、'.join(list(headers)[:3])}）")
    prof.page_offset = _detect_page_offset(book)

    # 1) 测量
    prof = probe(book, base=prof)
    lines = _candidate_lines(book)

    # 2) 假设 + 3) 打分
    for fam in FAMILIES:
        prof.family_scores[fam.key] = _score_family(book, fam, lines, prof.body_size)

    ranked = sorted(prof.family_scores.items(), key=lambda kv: -kv[1]["score"])
    best_key, best = ranked[0]
    # 主策略必须有实际命中，否则说明这本书没有任何可用的文字结构信号
    if best["hits"] >= 5 and best["score"] >= 0.45:
        prof.primary_family = best_key
        # 后备链：分数达到主策略 60% 的家族都启用（多家族共存是常态：
        # "第X章" 管一级，"1.2" 管二级，"表Ⅰ：" 管三级，它们不是竞争关系）
        prof.enabled_families = [
            k for k, v in ranked if v["hits"] >= 3 and v["score"] >= max(0.25, best["score"] * 0.6)
        ]
    else:
        prof.primary_family = ""
        prof.enabled_families = []
        prof.warnings.append("没有任何标题家族命中：只能按书签/目录页/字号聚类退化处理")

    # 书签兜底：书签本身就是最好的结构，哪怕家族没命中也能切
    if prof.has_bookmarks and not prof.enabled_families:
        prof.enabled_families = ["cjk_chapter"] if "cjk_chapter" in prof.family_scores else []
        prof.warnings.append("标题家族全部低分，改为完全按书签切章（置信度下调）")

    _guess_kind(book, prof)

    # 置信度：书签召回优先，其次靠自洽性
    if prof.has_bookmarks and prof.primary_family:
        prof.confidence = round(min(1.0, 0.5 + 0.5 * best["recall"]), 3)
    elif prof.has_toc_page:
        prof.confidence = round(min(0.85, 0.4 + 0.4 * best["consistency"]), 3)
    else:
        prof.confidence = round(min(0.6, 0.25 + 0.4 * best["consistency"]), 3)
        prof.warnings.append("无书签也无目录页：章节边界只能靠规约推断，建议人工抽查")

    if prof.doc_kind == "scanned":
        prof.confidence = min(prof.confidence, 0.2)

    if save_to:
        prof.save(save_to)
    return prof


def load_profile(path: str | Path) -> LayoutProfile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return LayoutProfile(**data)


def describe(prof: LayoutProfile) -> str:
    """给报告/命令行看的一段人话。"""
    lines = [
        f"《{prof.book_title}》 {prof.page_count} 页 | 类型={prof.doc_kind} | 置信度={prof.confidence}",
        f"  版面：正文左边界 {prof.body_left:.0f}pt，正文 {prof.body_size}pt，栏数 {prof.columns}，"
        f"平均行宽 {prof.avg_line_chars} 字",
        f"  文字层：每页 {prof.chars_per_page} 字，空白页 {prof.empty_page_ratio:.0%}",
        f"  结构证据：书签 {prof.bookmark_count} 条 {prof.bookmark_levels or '{}'}；"
        f"目录页 {prof.toc_pages or '无'}（{prof.toc_entry_count} 条）；表格 {prof.table_count} 张",
        f"  行文：引号行 {prof.dialog_ratio:.1%}，骰子行 {prof.dice_ratio:.1%}",
        f"  主策略：{prof.primary_family or '(无)'}；启用家族：{', '.join(prof.enabled_families) or '(无)'}",
        f"  印刷页码偏移：{prof.page_offset if prof.page_offset is not None else '未知'}"
        "（PDF 页 - 印刷页）",
    ]
    top = sorted(prof.family_scores.items(), key=lambda kv: -kv[1]["score"])[:4]
    lines.append("  家族打分：" + "，".join(f"{k}={v['score']}(命中{v['hits']})" for k, v in top))
    for n in prof.notes:
        lines.append(f"  · {n}")
    for w in prof.warnings:
        lines.append(f"  ⚠ {w}")
    return "\n".join(lines)
