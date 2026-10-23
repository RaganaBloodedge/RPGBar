"""PDF → 页 / 行 / 词。**这一层只负责"看得见什么"，不负责"这意味着什么"。**

为什么要跟 layout.py 分开：

- 不同版式的书（规则书 / 小说 / 技术手册 / 扫描件）都能被这一层读成同一种数据；
- 「这行是不是标题」「这张是不是表」属于**判断**，判断要能换策略、要能打分、要能解释，
  所以放在 layout.py 里，按画像驱动。

坐标约定：`y` 向下增长（PDF 原生坐标系），单位是 pt。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 页眉页脚带：正文不会落在这两条带子里。用比例而不是绝对值，A4/16开/letter 都适用。
HEADER_BAND = 0.075
FOOTER_BAND = 0.94


@dataclass
class Span:
    text: str
    size: float
    font: str
    bold: bool
    x0: float
    x1: float


@dataclass
class Line:
    """一行文本 = 一个 span 序列合并后的结果。

    `x0` 保留下来是为了**缩进信息**：中文书里段落首行缩进 2 字，标题/条目常常顶格或左缩进，
    这是没有字号差别的版式里最可靠的结构信号（COC 规则书正文与标题同为 10.6pt）。
    """

    page: int
    y: float
    x0: float
    x1: float
    text: str
    size: float
    font: str
    bold: bool
    spans: list[Span] = field(default_factory=list)

    @property
    def char_len(self) -> int:
        return len(self.text)

    @property
    def is_short(self) -> bool:
        """短行 = 标题候选。40 字是经验值：中文标题极少超过 40 字，正文行普遍 35-45 字。"""
        return len(self.text) <= 40

    @property
    def ends_sentence(self) -> bool:
        return bool(self.text) and self.text[-1] in "。！？…”』」!?."


@dataclass
class Page:
    no: int  # 1 起，给人看的页码（不是 PDF 里的打印页码）
    width: float
    height: float
    lines: list[Line] = field(default_factory=list)
    words: list[tuple] = field(default_factory=list)  # (x0, y0, x1, y1, text)
    # 页眉页脚带（由 layout 画像测出来再回填；默认 0~∞ = 不裁剪，宁可多不可少）
    band_top: float = 0.0
    band_bottom: float = 1e9
    # 页眉文字黑名单（归一化后无空白的整行文字）。由 layout 探测后回填。
    # 为什么要它：很多书的页眉第二行是"节名"（字号还比正文大），只在十几页重复，
    # 够不上"同一串文字出现 N 页"的带检测阈值，于是被当正文缝进块里。
    header_texts: frozenset = frozenset()

    # ---- 版式探针用到的三个派生量 ----
    def body_lines(self) -> list[Line]:
        """去掉页眉页脚之后的正文行。

        三层过滤，从粗到细：

        1. **带**：`band_top ~ band_bottom` 之外的一律不算（章名/页码这类全书重复的）；
        2. **页码**：靠 `is_page_number` 认（"12" / "· 45 ·" / "Page 7"）；
        3. **页眉黑名单**：顶部居中且在 ≥3 页重复的行（节名型页眉，字号常比正文大）。

        第 3 条是实测补的：p381 的「附录Ⅲ：物价表」是 16pt 居中行，出现在附录Ⅲ 的 7 页顶部，
        带检测（要求几十页重复）抓不到它，结果它被缝进了商品清单块里。
        """
        cached = self.__dict__.get("_body_lines_cache")
        if cached is not None:
            return cached
        top, bottom = self.band_top, self.band_bottom
        out: list[Line] = []
        for l in self.lines:
            if not (top <= l.y <= bottom) or is_page_number(l.text):
                continue
            if self.header_texts and _norm_line(l.text) in self.header_texts:
                continue
            out.append(l)
        self.__dict__["_body_lines_cache"] = out
        return out

    def margins(self) -> tuple[float, float]:
        """返回 (左边界, 右边界) 的众数：正文的"顶格"位置。"""
        lefts = [round(l.x0) for l in self.body_lines() if l.text]
        if not lefts:
            return 0.0, self.width
        from collections import Counter

        left = Counter(lefts).most_common(1)[0][0]
        rights = [round(l.x1) for l in self.body_lines()]
        right = Counter(rights).most_common(1)[0][0] if rights else self.width
        return float(left), float(right)

    def text(self) -> str:
        return "\n".join(l.text for l in self.body_lines())

    def reading_text(self) -> str:
        """**原文层**用的逐页文本：阅读顺序（先左栏后右栏）、不摊平表格、不改一个字。

        与 `reading_lines()` 的区别只有一个：这里保留原行序，用来给人看、给块回指。
        为什么不用 `text()`：那个按 y 排序，双栏页面上人读起来是断的。
        """
        return "\n".join(l.text for l in self.reading_lines())

    def column_bands(self, gap: float = 150.0) -> list[tuple[float, float]]:
        """页内文本栏 [x_lo, x_hi]。

        阈值是量出来的：正文双栏的**栏首间距** 260pt+，而表格内的列间距只有 50-130pt。
        取 30pt 会把属性表的 STR/90/5D6 拆成三栏（实测：怪物属性因此全丢），
        取 100pt 仍会把 58→163 的表格列误判成换栏，150pt 才稳。
        """
        xs = sorted({round(l.x0) for l in self.body_lines()})
        if not xs:
            return [(0.0, self.width)]
        # 只把"成规模"的行首当作栏证据：居中的页眉/章名/页码是孤例，不能凭它们造出栏边界。
        # 聚类容差 5pt（中文首行缩进 21pt 会被分成两簇，但两簇都在同一栏，不影响结论）
        vals = sorted(round(l.x0) for l in self.body_lines())
        clusters: list[list[int]] = []
        for v in vals:
            if clusters and v - clusters[-1][0] <= 5:
                clusters[-1][0] = v
                clusters[-1][1] += 1
            else:
                clusters.append([v, 1])
        xs = [c for c, n in clusters if n >= 3] or xs
        cuts: list[float] = []
        for a, b in zip(xs, xs[1:]):
            if b - a > gap:
                cuts.append((a + b) / 2)
        bands: list[tuple[float, float]] = []
        lo = 0.0
        for c in cuts:
            bands.append((lo, c))
            lo = c
        bands.append((lo, self.width))
        return bands

    def band_of(self, x: float) -> tuple[float, float]:
        for b in self.column_bands():
            if b[0] <= x < b[1]:
                return b
        return self.column_bands()[-1]

    def reading_lines(self) -> list[Line]:
        """按**人类的读法**返回正文行：**一栏读到底，再读下一栏**。

        为什么必须单独做这一步：PDF 里行是按 y 排的，双栏页面上左栏第 1 行和右栏第 1 行
        y 相同。按 (y, x) 排会把两栏**逐行交替**拼起来 —— 实测就是「潜水（01%） 机械维修
        （10%）」这种把两条技能缝成一条的鬼东西。
        """
        lines = self.body_lines()
        if not lines:
            return []
        bands = self.column_bands()
        buckets: list[list[Line]] = [[] for _ in bands]
        for ln in lines:
            lo, hi = self.band_of(ln.x0)
            for i, b in enumerate(bands):
                if b == (lo, hi):
                    buckets[i].append(ln)
                    break
            else:
                buckets[-1].append(ln)
        out: list[Line] = []
        for b in buckets:
            out.extend(sorted(b, key=lambda l: l.y))
        return out

    def tables(self) -> list[Table]:
        """本页表格（带缓存：一页会被切块、抽取、建索引各调用一次，没必要重算）。"""
        cached = getattr(self, "_tables", None)
        if cached is None:
            cached = detect_tables(self)
            self._tables = cached
        return cached


@dataclass
class Book:
    path: str
    title: str
    page_count: int
    pages: list[Page]
    bookmarks: list[tuple[int, str, int]] = field(default_factory=list)  # (level, title, page)
    # 书签的**落点 y**（页内位置）。同一个 PDF 页上常常挂着好几个书签（COC 的 5.7/5.8
    # 都在第 77 页），只有页码时这四节会各自把整页内容切一遍 —— 实测同一段 701 字被切成
    # 三份一模一样的块（"三胞胎"）。拿到 y 才能按页内阅读顺序切出互不重叠的区间。
    bookmark_anchors: list[tuple[int, str, int, float]] = field(default_factory=list)  # (level, title, page, y)

    def page(self, no: int) -> Page:
        return self.pages[max(0, min(no - 1, len(self.pages) - 1))]


_PAGE_NO_RE = re.compile(r"^[\s\-–—·]*[0-9ivxlcdmIVXLCDM]{1,5}[\s\-–—·]*$")


def is_page_number(text: str) -> bool:
    return bool(_PAGE_NO_RE.match(text or ""))


def _norm_line(text: str) -> str:
    """页眉比对用的归一化：去掉所有空白，避免"第十六章 附录"与"第十六章附录"被当成两行。"""
    return re.sub(r"\s+", "", text or "")


def set_bands(book: "Book", top: float, bottom: float, header_texts: frozenset | None = None) -> None:
    """把测出来的页眉页脚带应用到整本书（layout 画像测完立刻调用）。"""
    for p in book.pages:
        p.band_top = max(0.0, min(top, p.height * 0.3))
        p.band_bottom = min(p.height, max(bottom, p.height * 0.7))
        if header_texts is not None:
            p.header_texts = header_texts
        p._tables = None  # 分栏依赖正文行，正文行变了缓存就得作废
        p.__dict__.pop("_body_lines_cache", None)  # 正文行缓存同理


def load(path: str | Path, max_pages: int | None = None) -> Book:
    """读一本 PDF。不做 OCR —— 扫描件会在 layout 画像里被判成 scanned 并给出明确告警。"""
    import pymupdf

    doc = pymupdf.open(str(path))
    anchors: list[tuple[int, str, int, float]] = []
    try:
        raw_toc = doc.get_toc() or []
    except Exception:  # noqa: BLE001  书签坏了不该让整本书读不进来
        raw_toc = []
    bookmarks = [(int(lv), str(t).strip(), int(pg)) for lv, t, pg in raw_toc if t]
    try:
        # simple=False 才带目标点（含页内 y）。拿不到就留空，下游退化成"按页切"。
        for it in doc.get_toc(simple=False) or []:
            if len(it) < 3 or not it[1]:
                continue
            to = it[3] if len(it) > 3 else None
            y = -1.0
            if isinstance(to, dict):
                pt = to.get("to")
                y = float(getattr(pt, "y", -1.0)) if pt is not None else -1.0
            anchors.append((int(it[0]), str(it[1]).strip(), int(it[2]), y))
    except Exception:  # noqa: BLE001
        anchors = []

    n = doc.page_count if max_pages is None else min(doc.page_count, max_pages)
    pages: list[Page] = []
    for i in range(n):
        p = doc[i]
        page = Page(no=i + 1, width=float(p.rect.width), height=float(p.rect.height))
        d = p.get_text("dict")
        for blk in d.get("blocks", []):
            for ln in blk.get("lines", []):
                spans = [
                    Span(
                        text=s.get("text", ""),
                        size=float(s.get("size", 0)),
                        font=str(s.get("font", "")),
                        bold=bool(s.get("flags", 0) & 16),
                        x0=float(s.get("bbox", (0, 0, 0, 0))[0]),
                        x1=float(s.get("bbox", (0, 0, 0, 0))[2]),
                    )
                    for s in ln.get("spans", [])
                ]
                text = "".join(s.text for s in spans).strip()
                if not text:
                    continue
                bbox = ln.get("bbox", (0, 0, 0, 0))
                main = max(spans, key=lambda s: len(s.text.strip())) if spans else None
                page.lines.append(
                    Line(
                        page=i + 1,
                        y=float(bbox[1]),
                        x0=float(bbox[0]),
                        x1=float(bbox[2]),
                        text=text,
                        size=round(main.size, 1) if main else 0.0,
                        font=main.font if main else "",
                        bold=bool(main.bold) if main else False,
                        spans=spans,
                    )
                )
        # 行内顺序：PDF 的 blocks 顺序在双栏排版里不可靠，按 (y, x) 重排才接近人眼顺序
        page.lines.sort(key=lambda l: (round(l.y / 2), l.x0))
        page.words = [
            (float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4]))
            for w in p.get_text("words")
        ]
        pages.append(page)

    meta_title = ""
    try:
        meta_title = (doc.metadata or {}).get("title", "") or ""
    except Exception:  # noqa: BLE001
        pass
    title = meta_title.strip() or Path(path).stem
    doc.close()
    return Book(
        path=str(path), title=title, page_count=n, pages=pages,
        bookmarks=bookmarks, bookmark_anchors=anchors,
    )


# ---------------------------------------------------------------- 表格（自己算，不用 find_tables）
#
# 为什么不用 pymupdf 的 find_tables：一次遍历 418 页会在中途段错误（实测 2.0 版），
# 而且它只认有框线的网格。这里用「词坐标聚类」自己判：
#   同一 y → 一行；行内 x 间距大于阈值 → 分栏；连续多行共享同样的栏心 → 一张表。
# 无框线的三线表、用空格对齐的表也能吃到。

_COL_GAP = 14.0  # 同一行内两个词之间超过这个距离就算换了一栏（中文词距通常 <10）


@dataclass
class Table:
    page: int
    y0: float
    y1: float
    header: list[str]
    rows: list[list[str]]
    x0: float = 0.0
    x1: float = 0.0

    @property
    def header_text(self) -> str:
        return " | ".join(self.header)


def _row_cells(words: list[tuple]) -> list[str]:
    """把一行里的词按 x 间距聚成单元格。"""
    return [t for t, _ in _row_cells_x(words)]


def _row_cells_x(words: list[tuple]) -> list[tuple[str, float]]:
    if not words:
        return []
    ws = sorted(words, key=lambda w: w[0])
    cells, cur = [], [ws[0]]
    for prev, w in zip(ws, ws[1:]):
        if w[0] - prev[2] > _COL_GAP:
            cells.append(cur)
            cur = [w]
        else:
            cur.append(w)
    cells.append(cur)
    return [("".join(x[4] for x in sorted(c, key=lambda z: z[0])), min(x[0] for x in c)) for c in cells]


def detect_tables(page: Page, min_cols: int = 3, min_rows: int = 3) -> list[Table]:
    """在一页里找表。返回的 header 是首行（列名），下游拿它当字段字典的证据。

    **列的边界由表头定义**，不靠分栏猜：表头单元格给出每一列的 x 位置，正文行里
    位置偏离所有表头列 40pt 以上的单元格会被丢掉。这一步专治一种污染：
    左栏属性表的某一行，正好和右栏正文的某一行 y 相同，`_row_cells` 会把右栏那句话
    当成第四列塞进来（实测表头里出现过「实验室里，等待不知情的科学家…」）。
    """
    return _detect_in_words(page, page.words, min_cols, min_rows)


def _detect_in_words(page: Page, words: list[tuple], min_cols: int, min_rows: int) -> list[Table]:
    rows: list[list[tuple]] = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if rows and abs(w[1] - rows[-1][0][1]) <= 3.0:
            rows[-1].append(w)
        else:
            rows.append([w])

    tables: list[Table] = []
    buf: list[tuple[float, list[str], list[tuple]]] = []

    def flush() -> None:
        nonlocal buf
        if len(buf) < min_rows:
            buf = []
            return
        hdr_words = buf[0][2]
        body = buf[1:]
        # 1) 列位置**由数据行统计出来**，不靠表头（表头常被合并单元格/旁边栏的字污染）
        xs: list[float] = []
        usable = 0
        for _y, _cells, ws in body:
            cells = _row_cells_x(ws)
            if len(cells) >= min_cols:
                usable += 1
                xs.extend(x for _t, x in cells)
        if usable < min_rows - 1:
            buf = []
            return
        xs.sort()
        groups: list[list[float]] = []
        for x in xs:
            if groups and x - groups[-1][-1] <= 25:
                groups[-1].append(x)
            else:
                groups.append([x])
        need = max(2, int(usable * 0.55))
        cols = [sum(g) / len(g) for g in groups if len(g) >= need]
        if len(cols) < min_cols:
            buf = []
            return

        # 2) 表头：把表头那一行的**词**分别贴到最近的列上（这样被合并的单元格也能拆开）
        labels = [""] * len(cols)
        for w in hdr_words:
            j = min(range(len(cols)), key=lambda k: abs(cols[k] - w[0]))
            if abs(cols[j] - w[0]) <= 45:
                labels[j] = (labels[j] + " " + w[4]).strip() if labels[j] else w[4]
        # 3) 表头必须"像标签"：太长、带逗号句号的，是把旁边栏的正文当成表头了。
        #    一列都不像标签 ⇒ 这不是表，是分栏排版的正文（物价表就是这种），直接放弃。
        keep = [
            j
            for j in range(len(cols))
            if labels[j] and len(labels[j]) <= 14 and not re.search(r"[，。；、]", labels[j])
        ]
        if len(keep) < min_cols:
            buf = []
            return

        rows: list[list[str]] = []
        for _y, _cells, ws in body:
            slot = ["" for _ in cols]
            for text, x in _row_cells_x(ws):
                j = min(range(len(cols)), key=lambda k: abs(cols[k] - x))
                if abs(cols[j] - x) <= 40 and not slot[j]:
                    slot[j] = text.strip()
            if sum(1 for c in slot if c) >= max(2, len(cols) // 2):
                rows.append([slot[j] for j in keep])
        if len(rows) < min_rows - 1:
            buf = []
            return
        words_all = [w for _y, _c, ws in buf for w in ws]
        tables.append(
            Table(
                page=page.no,
                y0=buf[0][0],
                y1=buf[-1][0],
                header=[labels[j].strip() for j in keep],
                rows=rows,
                x0=min(w[0] for w in words_all),
                x1=max(cols[j] for j in keep),
            )
        )
        buf = []

    for r in rows:
        cells = _row_cells(r)
        if len(cells) >= min_cols and all(c.strip() for c in cells):
            buf.append((r[0][1], cells, r))
        else:
            flush()
    flush()
    return tables
