"""粗分：把书切成章节树；细分：把章节切成块。

两条分工要说清楚：

- **粗分（section）** 是"书的结构"：目录 / 书签 / 编号 / 字号，谁有先用谁。它的产物是
  一棵带页码范围树，用来给每块知识标"出处"（DM 引用时要能说清"第六章 6.2 常规接触战斗"）。
- **细分（block）** 是"知识的单位"：一条技能、一只怪物、一张表、一段世界观。它不追求
  和书的结构对齐 —— 一页里可能有三条技能，也可能一条技能跨两页。

所以这里不做"按页切"（那是 RAG 的老毛病：切在句子中间、把一条规则劈成两半），
而是先按**行距/缩进**找段落边界，再按**条目头**（顶格短行 + 后接缩进正文）找条目边界。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .layout import LayoutProfile, in_lines, _similar
from .reader import Book, Line, Table, detect_tables

# ---------------------------------------------------------------- 章节

@dataclass
class Section:
    id: str
    level: int
    title: str
    page_start: int
    page_end: int
    parent_id: str | None = None
    order: int = 0
    source: str = "bookmark"  # bookmark / toc_page / heading
    # 页内锚点：书签的落点 y。**同一页挂多个书签时，只有它能把四节分开**。
    anchor_y: float | None = None
    # 下一个条目的锚点（用来在"同页多节"时截断本节）
    end_page: int | None = None
    end_y: float | None = None
    end_title: str = ""

    @property
    def pages(self) -> range:
        return range(self.page_start, self.page_end + 1)

    @property
    def shared_start_page(self) -> bool:
        """本节与下一个条目共用起始页（页内要按行区间切）。"""
        return self.end_page is not None and self.end_page == self.page_start


@dataclass
class Block:
    """检索与抽取的最小单元。`text` 永远是**原文**，一个字都不改写。"""

    id: str
    section_id: str
    order: int
    page: int
    page_end: int
    title: str
    text: str
    lines: list[Line] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    kind: str = ""
    subtype: str = ""
    confidence: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def char_len(self) -> int:
        return len(self.text)


def section_id(book_idx: int, order: int) -> str:
    return f"s{book_idx:04d}-{order:03d}"


def build_sections(book: Book, prof: LayoutProfile, book_id: str) -> list[Section]:
    """三种来源，按可信度排序：书签 > 目录页 > 版式标题。

    **书签带落点 y 时，章节区间按"页 + 页内行区间"算，不再只是页码区间。**
    这不是锦上添花：COC 这本书的 5.6/5.7/5.8/5.8.1/5.8.2 五个条目的书签全落在第 77 页，
    只按页码算时五节各自把整页切一遍 —— 实测同一段 701 字被切成三份一模一样的块，
    而且每块的开头还混进了上一节的内容。拿到 y 之后，每节只吃自己那一段。
    """
    raw: list[tuple[int, str, int, str, float | None]] = []  # (level, title, page, source, y)

    if book.bookmark_anchors:
        raw = [
            (int(lv), t, int(pg), "bookmark", (float(y) if y is not None and y >= 0 else None))
            for lv, t, pg, y in book.bookmark_anchors
        ]
    elif book.bookmarks:
        raw = [(int(lv), t, int(pg), "bookmark", None) for lv, t, pg in book.bookmarks]
    if not raw and prof.has_toc_page:
        raw = [(lv, t, pg, src, None) for lv, t, pg, src in _sections_from_toc_page(book, prof)]
    if not raw:
        raw = [(lv, t, pg, src, None) for lv, t, pg, src in _sections_from_headings(book, prof)]

    # 清洗：页码范围夹到 [1, page_count]，丢掉明显跑偏的条目
    clean: list[tuple[int, str, int, str, float | None]] = []
    for lv, title, pg, src, y in raw:
        pg = max(1, min(pg, book.page_count))
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        clean.append((max(1, min(int(lv), 4)), title, pg, src, y))

    # **稳定排序，只按页**：书签/目录本身就是"人读的顺序"，同一页内的先后就是页内从上到下。
    # 早先按 (page, level) 排，把同一页的 5.8 排到了 5.7 前面，于是 5.8 的区间从
    # 5.7 的正文开始 —— 这就是"标题是 5.8、正文是 5.7"的缝合来源。
    clean.sort(key=lambda x: x[2])

    # 同一页上重复的同一标题（章首大标题 + 书签）只留一个
    dedup: list[tuple[int, str, int, str, float | None]] = []
    for item in clean:
        if dedup and item[2] == dedup[-1][2] and _similar(item[1], dedup[-1][1]) > 0.85:
            continue
        dedup.append(item)

    sections: list[Section] = []
    for i, (lv, title, pg, src, y) in enumerate(dedup):
        nxt = dedup[i + 1] if i + 1 < len(dedup) else None
        nxt_pg = nxt[2] if nxt else book.page_count + 1
        nxt_y = nxt[4] if nxt else None
        has_y = y is not None and y >= 0
        if nxt is None:
            page_end = book.page_count
        elif nxt_pg > pg:
            # 两端的书签都带页内落点 → 本节的终点是**下一节锚点所在页上的那一行**，
            # 所以那一页也要算进来（尾部按行裁掉）。只按页码算会把 5.8 的第一页整页
            # 判给 5.7，结果是"5.8 一个块都没有、内容全在 5.7 里"。
            page_end = nxt_pg if (has_y and nxt_y is not None and nxt_y >= 0) else nxt_pg - 1
        else:
            page_end = pg
        page_end = max(pg, min(page_end, book.page_count))
        end_page = nxt_pg if (nxt is not None and nxt_pg <= page_end and nxt_pg <= book.page_count) else None
        sections.append(
            Section(
                id=section_id(0, i),
                level=lv,
                title=title,
                page_start=pg,
                page_end=page_end,
                order=i,
                source=src,
                anchor_y=y,
                end_page=end_page,
                end_y=nxt_y,
                end_title=(nxt[1] if nxt else ""),
            )
        )
    # 父子关系：向上找最近一个层级更小的条目
    stack: list[Section] = []
    for s in sections:
        while stack and stack[-1].level >= s.level:
            stack.pop()
        s.parent_id = stack[-1].id if stack else None
        stack.append(s)
    return sections


# ---------------------------------------------------------------- 页内锚点裁切


def _norm_title(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def title_index(lines: list, title: str, y: float | None = None) -> int | None:
    """在**一页的阅读顺序行**里找到某个条目标题的起始行号。

    优先按文字认（书签标题一般就是印出来的标题），认不出再退回书签落点 y。
    返回 None = 认不出；调用方据此退化成"按页切"，不硬猜。
    `lines` 可以是 Line 对象列表，也可以是纯字符串列表。
    """
    if not lines:
        return None
    t = _norm_title(title)
    if t:
        for i, ln in enumerate(lines):
            txt = getattr(ln, "text", ln)
            lt = _norm_title(txt)
            if not lt:
                continue
            if lt == t:
                return i
            if _similar(str(txt), title) >= 0.62 and len(lt) <= max(24, int(len(t) * 1.6)):
                return i
    if y is not None and y >= 0:
        for i, ln in enumerate(lines):
            ly = getattr(ln, "y", None)
            if ly is not None and ly >= y - 3:
                return i
    return None


def clip_lines(book: Book, sec: Section, pno: int, lines: list) -> list:
    """把一页的阅读顺序行裁到本节区间。

    只在"本节与下一个条目共用同一页"时才有实际裁切；其余情况原样返回。
    """
    if not lines:
        return lines
    lo, hi = 0, len(lines)
    if pno == sec.page_start:
        i = title_index(lines, sec.title, sec.anchor_y)
        if i is not None:
            lo = i
    if sec.end_page is not None and pno == sec.end_page:
        j = title_index(lines, sec.end_title, sec.end_y)
        if j is not None:
            hi = max(lo, j)
    if lo == 0 and hi == len(lines):
        return lines
    return lines[lo:hi]


_TOC_ENTRY_RE = re.compile(r"^(?P<title>.+?)\s*[.．·…\s]{2,}(?P<page>\d{1,4})\s*$")


def _sections_from_toc_page(book: Book, prof: LayoutProfile) -> list[tuple[int, str, int, str]]:
    """从印刷目录页反解章节：标题 + 点线 + 印刷页码 → 加偏移换成 PDF 页。"""
    offset = prof.page_offset or 0
    out: list[tuple[int, str, int, str]] = []
    for pno in prof.toc_pages:
        page = book.page(pno)
        lefts = sorted({round(l.x0) for l in page.body_lines() if l.text})
        for ln in page.body_lines():
            m = _TOC_ENTRY_RE.match(ln.text)
            if not m:
                continue
            title = re.sub(r"[.．·…\s]+$", "", m.group("title")).strip()
            printed = int(m.group("page"))
            level = 1 if re.match(r"^第[〇零一二三四五六七八九十百0-9]+[章篇部]", title) else 2
            # 用缩进把层级再校一遍：缩进越深层级越低
            if len(lefts) >= 2 and round(ln.x0) > lefts[0] + 8:
                level = min(3, level + 1)
            out.append((level, title, printed + offset, "toc_page"))
    return out


def _sections_from_headings(book: Book, prof: LayoutProfile) -> list[tuple[int, str, int, str]]:
    """没有任何目录信息时的兜底：只用画像里"考过试"的家族。"""
    fams = prof.families()
    if not fams:
        return []
    out: list[tuple[int, str, int, str]] = []
    for p in book.pages:
        for ln in p.body_lines():
            for f in fams:
                if not f.match(ln, prof.body_size):
                    continue
                # font_cluster 只能当二级：一级标题（章）需要编号或大字号两条同时成立
                lv = f.level
                if f.key == "font_cluster":
                    lv = 1 if ln.size >= prof.body_size + 4 else 2
                out.append((lv, ln.text, p.no, "heading"))
                break
    return out


# ---------------------------------------------------------------- 块

_ENTRY_TAIL = re.compile(r"[:：]$")
_BULLET = re.compile(r"^[·•\-\u2022]\s*")
_DICE_OR_NUM = re.compile(r"\d")

# ---------------------------------------------------------------- 清单（第三类版式）
#
# 规则书里有第三类版式，既不是"段落"也不是"表格"，而是**清单**：
#   精纺毛料礼服，$17.95
#   林场连衫裤，69 美分
#   风衣，$9.95-35.00
# 一行一个条目。第一版把它当段落处理，段落合并用 "".join 把几十个商品粘成 1000 字一坨，
# 于是"各种商品的价格"看起来就是"未切分"。切分必须认这一种边界。
_PRICE = r"(?:[$￥]\s*[\d.,]+(?:-\d[\d.,]*)?|\d+(?:\.\d+)?\s*(?:美分|美元|便士|先令|英镑))"
# 物品型表的表头特征：有名字列 + 有物品属性列
# 物品型表：要么有"名称"列，要么有物品才有的列（价格/射程/弹数/故障/口径）。
# 不能只认"名称"—— 这本书的武器表表头是"所用技能|伤害|贯穿|基础射程|每轮|弹数|价格($)"，
# 根本没有"名称"两个字；也不能只认"伤害"—— 伤害加值表会误判。
_ITEM_TABLE_RE = re.compile(r"(名称|物品|武器|装备|道具|载具|价格|射程|弹数|故障|口径|载弹)")
_LIST_LINE_RE = re.compile(rf"^(?P<name>[^，,。；：]{{1,40}})[，,]\s*(?P<price>{_PRICE}.*)$")
# 价格"从这个位置开始"的判据（parse_list_line 从右往左找它）
_PRICE_HEAD = re.compile(rf"^(?:[$￥]|[\d.,]+\s*(?:美分|美元|便士|先令|英镑))")


# ---------------------------------------------------------------- 术语表（glossary）
#
# 术语表是第四种形态：`术语(English)：解释…p103`，一条跨 2-4 行，条目之间**没有**空行、
# 没有缩进变化、没有字号变化。段落合并会把整页粘成一块（实测 p373 = 1867 字一整块）。
# 唯一可靠的边界是"术语 + 冒号"这个开头，所以只在**术语/词汇类章节**里按它切。
_TERM_HEAD_RE = re.compile(r"^(?P<term>[^。，,；;：:]{2,18}?)\s*(?:[（(][^）)]{0,24}[）)])?\s*[：:]")


def is_glossary_section(title: str) -> bool:
    return bool(re.search(r"(术语|词汇|用语|名词表|glossary)", title or "", re.I))


def _split_glossary(lines: list) -> list[tuple[str, list]]:
    """把术语表切成 [(术语, 行列表)]。切不动就返回空列表（交回普通切法）。"""
    heads: list[int] = []
    for i, ln in enumerate(lines):
        m = _TERM_HEAD_RE.match(ln.text.strip())
        if m and len(m.group("term").strip()) >= 2:
            heads.append(i)
    if len(heads) < 3:
        return []
    out: list[tuple[str, list]] = []
    for n, start in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        term = _TERM_HEAD_RE.match(lines[start].text.strip()).group("term").strip()
        out.append((term, lines[start:end]))
    return out


# 行内条目头：`名称（NN%）`。技能列表里一半条目是这种写法 ——
#   说服（10%）使用说服来通过一场有理有据的…      （名字和描述在同一行）
#   射击的专业：弓（15%）：用来使用弓以及弩…        （名字里还带冒号）
# 靠"独立短行"的规则抓不到，只能按"名称 + 百分比"这个特征在块内部再切一刀。
_INLINE_HEAD_RE = re.compile(r"(?P<name>[^\n。；;：:]{2,24}?)\s*[（(]\s*\d{1,3}\s*%\s*[）)]")
_ENTRY_LIST_SECTION = re.compile(r"(列表|技能|法术|专长|装备|武器)")


def _resplit_inline_heads(blocks: list, max_len: int = 240) -> list:
    """把"一个块里塞了好几条条目"的块按 `名称（NN%）` 再切开。

    只在"条目列表型"章节里做（技能列表 / 法术列表 / 装备），因为正文里也会出现
    "（考古学 01%）"这种引用，全局切会把叙述切碎。
    """
    out = []
    for b in blocks:
        text = b.text or ""
        hits = list(_INLINE_HEAD_RE.finditer(text))
        # 至少 2 个条目头、且块确实偏长，才值得切
        if len(hits) < 2 or len(text) <= max_len:  # 太短的块本来就只有一条
            out.append(b)
            continue
        cuts = [m.start() for m in hits if m.start() > 0]
        cuts = sorted(set([0] + cuts + [len(text)]))
        for k in range(len(cuts) - 1):
            seg = text[cuts[k] : cuts[k + 1]].strip()
            if not seg:
                continue
            m = _INLINE_HEAD_RE.match(seg) or _INLINE_HEAD_RE.search(seg[:60])
            title = (m.group("name").strip() if m else b.title)[:24] or b.title
            out.append(
                Block(
                    id=f"{b.id}-h{len(out):03d}",
                    section_id=b.section_id,
                    order=b.order,
                    page=b.page,
                    page_end=b.page_end,
                    title=title,
                    text=seg,
                    lines=b.lines,
                    meta=dict(b.meta or {}),
                )
            )
    return out


def parse_list_line(text: str) -> tuple[str, str] | None:
    """「名称，价格」→ (名称, 价格)；不是这种形状返回 None。

    **名字里允许带逗号**，价格从右往左找。实测书名里真有这种条目：

        卷发棒，小卷，$2.19          ← 名字是"卷发棒，小卷"，不是"卷发棒"
        博洛尼亚香肠、法兰克福香肠，19 美分

    早先的正则只认"第一个逗号左边是名字"，于是这类行解析失败、掉进段落合并，
    把几十条商品粘成一个 472 字的块（p381 实测）。改成从右往左找第一个"以价格开头"的
    逗号分段，左边整段都当名字 —— 平手时靠右的赢，正是我们要的。
    """
    t = (text or "").strip()
    if not t or len(t) > 80:
        return None
    parts = re.split(r"[，,]", t)
    if len(parts) < 2:
        return None
    for i in range(len(parts) - 1, 0, -1):
        price = parts[i].strip()
        if not _PRICE_HEAD.match(price):
            continue
        name = "，".join(parts[:i]).strip().strip("（）()【】[] ")
        if len(name) < 2:
            return None
        if len(name) > 40:  # 太长的是句子，不是商品名
            return None
        return name, price
    return None


def _find_list_runs(lines: list) -> tuple[dict[int, dict], int]:
    """找出"连续 ≥3 行都是 名称，价格"的区段。

    返回 (跳过集合, 条目数)：跳过集合的 key 是行下标，值是 {name, price, group}。
    `group` 取区段前面那行短标题（"男装"/"医疗用品"/"工具"），当物品分类用。
    """
    out: dict[int, dict] = {}
    n_items = 0
    i = 0
    while i < len(lines):
        if not parse_list_line(lines[i].text):
            i += 1
            continue
        j = i
        while j < len(lines) and parse_list_line(lines[j].text):
            j += 1
        if j - i >= 3:  # 连续 3 行以上才算清单，避免正文里偶然一句"…，$5"
            # 分类名：往回找最近一条"不是条目"的短行（"男装"/"医疗用品"/"梳妆用品"）。
            # 不能只看紧邻上一行 —— 上一行可能是个自带逗号的条目（"卷发棒，小卷，$2.19"），
            # 那样会把商品名当成分组。
            group = ""
            for back in range(1, 4):
                if i - back < 0:
                    break
                prev = lines[i - back].text.strip()
                # 既跳过条目，也跳过"名字里带逗号导致解析不出来、但含价格"的行
                if parse_list_line(prev) or re.search(_PRICE, prev):
                    continue
                if 2 <= len(prev) <= 12 and prev[-1] not in "。，,；：":
                    group = prev
                break
            for k in range(i, j):
                name, price = parse_list_line(lines[k].text)
                out[k] = {"name": name, "price": price, "group": group}
                n_items += 1
        i = j
    return out, n_items


@dataclass
class _Para:
    lines: list[Line]
    y0: float
    y1: float

    @property
    def text(self) -> str:
        return "".join(l.text for l in self.lines)

    @property
    def page(self) -> int:
        return self.lines[0].page

    @property
    def page_end(self) -> int:
        return self.lines[-1].page


def _is_price_tail(text: str) -> bool:
    """这一行是不是"被折断的商品名的后半截"。

    判据只有一条：**价格前面剩下的字 ≤ 4 个**。

        衣)，$19.95                  → 价格前只有"衣)" 2 字 → 是尾巴，要接回去
        精纺毛料礼服，$17.95          → 价格前 6 字 → 完整条目，不许接

    这条阈值是实测定出来的：早先只判"含价格且很短"，结果把分组标题「男装」和
    它下面的商品并成了一条（"男装精纺毛料礼服"），块数从 73 掉到 50。
    """
    for seg in reversed(re.split(r"[，,]", (text or "").strip())):
        if _PRICE_HEAD.match(seg.strip()):
            head = re.split(r"[，,]", (text or "").strip())
            idx = [i for i, s in enumerate(head) if s.strip() == seg.strip()]
            if not idx:
                return False
            return len("".join(head[: idx[0]]).strip()) <= 4
    return False


def merge_wrapped_lines(lines: list[Line]) -> list[Line]:
    """断行续接：把被排版折断的一行接回去。

    实测：`（狗皮大` / `衣)，$19.95` —— 商品名被折成两行，直接切会得到一个叫"衣)"的商品。
    判据（两边都要成立才接）：
      - 上一行**不以句末标点/右括号结尾**；
      - 下一行**以右括号开头**，或者它是个清单行但名字短到 ≤3 字（明显是上半截的尾巴）。
    """
    from dataclasses import replace as _replace

    out: list[Line] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            t, nt = cur.text.strip(), nxt.text.strip()
            same_page = nxt.page == cur.page
            next_is_tail = (
                nt.startswith(("）", ")"))
                or (lambda g: bool(g) and len(g[0]) <= 3)(parse_list_line(nt))
                or _is_price_tail(nt)
            )
            if (
                same_page
                and t
                and not t.endswith(("。", "！", "？", "…", "：", "；", ")", "）", "，", ","))
                and next_is_tail
            ):
                out.append(_replace(cur, text=t + nt, x1=max(cur.x1, nxt.x1)))
                i += 2
                continue
        out.append(cur)
        i += 1
    return out


def _paragraphs(lines: list[Line]) -> list[_Para]:
    """行 → 段落。判据只有三条：换页、行距变大、行首缩进变化（首行缩进 vs 顶格续行）。

    额外要处理一个双栏特有的坑：**一个段落跨栏**。左栏最后一行没写完，句子在右栏顶部
    接着写。按"换栏 = 新段"会把一句话劈成两半（实测产出「…失败，他可」+「能被发现…」
    这种断头标题）。所以换栏时再看一眼：如果上一行没有句末标点、且新栏第一行正是该栏的
    续行位置（栏内最小 x），就认为它是续写，不切段。
    """
    if not lines:
        return []
    gaps = [b.y - a.y for a, b in zip(lines, lines[1:]) if b.page == a.page and b.y > a.y]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 18.0

    def run_min_x(start: int) -> float:
        """从 start 开始、同一栏内（y 递增）的最小 x0 = 该栏的"续行位置"。"""
        m = lines[start].x0
        y = lines[start].y
        for ln in lines[start + 1 :]:
            if ln.page != lines[start].page or ln.y < y - 5:
                break
            m = min(m, ln.x0)
            y = ln.y
        return m

    paras: list[_Para] = []
    cur: list[Line] = []
    for i, (prev, ln) in enumerate(zip(lines, lines[1:])):
        same_page = ln.page == prev.page
        new_col = same_page and (ln.y - prev.y) < -30  # y 倒退 = 换栏（或换页）
        gap = ln.y - prev.y
        continued = False
        if new_col:
            carried = not prev.text.rstrip().endswith(("。", "！", "？", "”", "』", "」", "…"))
            if carried and abs(ln.x0 - run_min_x(i + 1)) <= 4:
                continued = True
        cur.append(prev)
        new_para = not same_page or (not continued and (
            new_col
            or gap > pitch * 1.45
            or (abs(ln.x0 - prev.x0) > 6 and ln.x0 > prev.x0)  # 续行顶格 → 新段首行缩进
        ))
        if new_para:
            paras.append(_Para(cur, cur[0].y, cur[-1].y))
            cur = []
    if lines:
        cur.append(lines[-1])
        paras.append(_Para(cur, cur[0].y, cur[-1].y))
    return paras


def _is_entry_head(para: _Para, body_left: float, body_size: float) -> bool:
    """条目头判据：**单行、短、不像句子、字号不小于正文**。

    COC 的技能条目（"考古学（01%）"）、法术条目（"束缚术"）、怪物名（"拜亚基，星骏"）
    都是这个形态。注意**不能用缩进判断** —— 实测这本书里条目名和它的说明段起始 x 相同
    （都是 58pt），靠缩进会把 100 条技能缝成一条。所以只认"短 + 不带句末标点"。
    """
    if len(para.lines) != 1:
        return False
    txt = para.text.strip()
    if not txt or len(txt) > 30:
        return False
    if txt[-1] in "。！？，、；：":
        return False
    if _BULLET.match(txt):  # 项目符号是列表项，不是条目头
        return False
    if para.lines[0].size < body_size - 0.1:
        return False
    # 属性数值行（"哈维：敏捷55"）不是条目头：它是**上一个条目的属性**。
    # 认成条目头，就会把下一页的正文缝在一个假名字下面 —— 实测"标题：哈维：敏捷55、
    # 正文：下页的射击规则"，看起来像一条规则，其实两件事。
    if re.search(r"[：:]\s*[^\s：:]{1,8}\d{1,3}\s*$", txt):
        return False
    # 摊平后的表行（"STR | 90 | （5D6×5）"）也不是条目头
    if "|" in txt or "｜" in txt:
        return False
    # 题词出处（"——H．P．洛夫克拉夫特，《魔宴》"）不是条目头：它是上一段的署名行。
    # 认成条目头会造出叫"——H.P.洛夫克拉夫特，《魔宴》"的块，实体抽取还会把它当 NPC。
    if txt.startswith(("——", "—", "-", "–", "~", "～")):
        return False
    if re.match(r"(STR|CON|SIZ|DEX|INT|POW|APP|EDU|LUK|LUCK)\b", txt, re.I):
        return False
    # 纯数字/纯符号行（页码、装饰线）不算
    return bool(re.search(r"[\u4e00-\u9fa5A-Za-z]", txt))


def split_blocks(book: Book, sec: Section, prof: LayoutProfile, max_chars: int = 520) -> list[Block]:
    """一个章节 → 若干块。

    规则：
    1. 章节内出现的**三级标题**（表Ⅰ：/图/附录 之类）本身切成一块（它们是要被引用的表）；
    2. 条目头 + 其后的正文段落 = 一块（技能/法术/怪物/物品都靠这条）；
    3. 其余连续段落按 `max_chars` 合并，避免一块只有一句话、检索时上下文太薄。
    """
    lines: list[Line] = []
    for pno in sec.pages:
        lines.extend(clip_lines(book, sec, pno, book.page(pno).reading_lines()))
    if not lines:
        return []

    tables: list[Table] = []
    for pno in sec.pages:
        tables.extend(detect_tables(book.page(pno)))

    # 属性表（"属性 | 平均 | 掷骰"三列、行首是 STR/CON/…）**不是独立表格，是怪物的属性块**。
    # 早先它被当普通表切成一堆 23 字的行块（标题就叫 "STR"），于是：① 库里多出 228 个
    # 属性行块；② 抽取器把行块标题当成怪物名，造出 INT/POW 这类伪实体；③ 怪物条目本身
    # 反而没有属性。这里把它摘出来，稍后**并回条目块**。
    stat_tables = [t for t in tables if _is_statblock_table(t)]
    tables = [t for t in tables if not _is_statblock_table(t)]
    all_tables = tables + stat_tables

    # 表格区域里的行不要混进正文块：表会被单独切成一块，混着放会把句子切碎
    # （实测：一张武器表能产出 30 条「1D6 | 10 | 1/4」这种垃圾正文）
    def in_table(ln: Line) -> bool:
        cx = (ln.x0 + ln.x1) / 2
        for t in all_tables:
            if t.page != ln.page or not (t.y0 - 3 <= ln.y <= t.y1 + 12):
                continue
            if t.x0 - 8 <= cx <= t.x1 + 8:
                return True
            # 表格右边缘常被"数据行列位统计"截短：属性表的掷骰列（（5D6×5））就落在
            # t.x1 外面，漏掉它会拼出一条"掷骰（5D6×5）（3D6×5）…"的假正文。
            # 只在"表体内 + 表右侧 + 短片段"三个条件同时成立时才吞，避免误伤右栏正文。
            if ln.x0 >= t.x0 - 8 and cx <= t.x1 + 100 and len(ln.text) <= 14:
                return True
        return False

    lines = [ln for ln in lines if not in_table(ln)]

    # 断行续接 → 再找清单
    lines = merge_wrapped_lines(lines)

    # 清单先摘出去：它们要一行一块，不能进段落合并
    raw_lines = list(lines)
    list_items, _n = _find_list_runs(raw_lines)
    if list_items:
        lines = [ln for i, ln in enumerate(raw_lines) if i not in list_items]

    paras = _paragraphs(lines)
    blocks: list[Block] = []

    # 术语表：一条一块（术语 + 解释 + 页码引用），否则整页会粘成 1800 字一块
    if is_glossary_section(sec.title):
        entries = _split_glossary(lines)
        if entries:
            for term, ls in entries:
                blocks.append(
                    Block(
                        id=f"{sec.id}-g{len(blocks):03d}",
                        section_id=sec.id,
                        order=len(blocks),
                        page=ls[0].page,
                        page_end=ls[-1].page,
                        title=term,
                        text="\n".join(l.text for l in ls),
                        lines=ls,
                        meta={"glossary": True, "force_kind": "lore", "force_subtype": "术语"},
                    )
                )
            return blocks

    def flush(title: str, ps: list[_Para]) -> None:
        if not ps:
            return
        text = "\n".join(p.text.strip() for p in ps if p.text.strip())
        if not text:
            return
        blocks.append(
            Block(
                id=f"{sec.id}-b{len(blocks):03d}",
                section_id=sec.id,
                order=len(blocks),
                page=ps[0].page,
                page_end=ps[-1].page_end,
                title=title,
                text=text,
                lines=[l for p in ps for l in p.lines],
            )
        )

    title = sec.title
    buf: list[_Para] = []
    prev_closed = True  # 上一个段落是否"把话说完了"（条目头只可能出现在这种位置之后）
    for para in paras:
        txt = para.text.strip()
        # 章首大标题重复出现（页内标题 + 书签同名）→ 不当作正文
        if _similar(txt, sec.title) > 0.9 and len(txt) <= 40:
            continue
        if prev_closed and _is_entry_head(para, prof.body_left, prof.body_size):
            flush(title, buf)
            buf = []
            title = txt
            prev_closed = True
            continue
        buf.append(para)
        prev_closed = txt.rstrip().endswith(("。", "！", "？", "”", "』", "」", "…", "："))
        if sum(len(p.text) for p in buf) >= max_chars:
            flush(title, buf)
            buf = []
            title = sec.title
    flush(title, buf)

    # 清单条目：一行一块，title 就是商品名（这样"开司米礼服"能被按名字直接查到）
    for idx in sorted(list_items):
        it = list_items[idx]
        ln = raw_lines[idx]
        blocks.append(
            Block(
                id=f"{sec.id}-i{len(blocks):03d}",
                section_id=sec.id,
                order=len(blocks),
                page=ln.page,
                page_end=ln.page,
                title=it["name"],
                text=ln.text,
                lines=[ln],
                meta={
                    "list_item": True,
                    "name": it["name"],
                    "price": it["price"],
                    "group": it["group"],
                    "force_kind": "tool",
                    "force_subtype": it["group"] or "价格",
                },
            )
        )

    # 条目列表型章节：块内若还塞着多条 `名称（NN%）`，按条目头再切一刀
    if _ENTRY_LIST_SECTION.search(sec.title):
        blocks = _resplit_inline_heads(blocks)

    # 表：**一行一块**（每一行就是一件武器/一个症状/一条价格，和清单同粒度）。
    # 整表一块时，BM25 命中的是一坨 16 行的文本，分不清是哪件武器；而抽实体时也是逐行抽的，
    # 两边粒度对齐才不会"实体有、检索没有"。行数 <3 的小表保持整块。
    for t in tables:
        if len(t.rows) >= 3 and len(t.header) >= 2:
            for ri, row in enumerate(t.rows):
                name = (row[0] or "").strip() or f"行{ri + 1}"
                text = "\n".join(
                    f"{h}：{v}" for h, v in zip(t.header, row) if (v or "").strip()
                ) or " | ".join(row)
                blocks.append(
                    Block(
                        id=f"{sec.id}-r{len(blocks):03d}",
                        section_id=sec.id,
                        order=len(blocks),
                        page=t.page,
                        page_end=t.page,
                        title=name,
                        text=text,
                        meta={
                            "is_table": True,
                            "table_title": f"{sec.title}·表",
                            "header": t.header,
                            "row": row,
                            # 只有"物品型表"（表头像物品清单）才当工具类；
                            # 伤害加值表/症状表这类**参考表**必须交给分类器按章节判，
                            # 否则会造出叫"2-64"的物品。
                            "item_table": bool(_ITEM_TABLE_RE.search(" ".join(t.header))),
                        },
                    )
                )
            continue
        blocks.append(
            Block(
                id=f"{sec.id}-t{len(blocks):03d}",
                section_id=sec.id,
                order=len(blocks),
                page=t.page,
                page_end=t.page,
                title=f"{sec.title}·表",
                text=t.header_text + "\n" + "\n".join(" | ".join(r) for r in t.rows[:200]),
                tables=[t],
                meta={"is_table": True, "cols": len(t.header)},
            )
        )
    if stat_tables:
        blocks = _attach_statblocks(book, blocks, stat_tables)
    return blocks


# ---------------------------------------------------------------- 属性表（怪物属性块）

_ABBR_SET = {"STR", "CON", "SIZ", "DEX", "INT", "POW", "APP", "EDU", "LUK", "LUCK"}
_STAT_HDR = re.compile(r"^(属\s*性|能力|属性值)$")


def _is_statblock_table(t: Table) -> bool:
    """这张表是不是"属性 | 平均 | 掷骰"式的怪物属性块。

    判据全部来自内容，不看章节名：表头有"属性/能力"且带"平均/掷骰"，且数据行里
    至少三行的首格是属性缩写。换一本用 DND 六属性或自定义属性的书，只要版式同形就认。
    """
    hdr = [(h or "").strip() for h in (t.header or [])]
    if not any(_STAT_HDR.match(h) for h in hdr):
        return False
    if not any(("平均" in h) or ("掷骰" in h) for h in hdr):
        return False
    rows = [r for r in (t.rows or []) if any((c or "").strip() for c in r)]
    if len(rows) < 3:
        return False
    abbr = sum(1 for r in rows if str(r[0] or "").strip().upper() in _ABBR_SET)
    return abbr >= 3


def _statblock_text(t: Table) -> tuple[str, dict]:
    """属性表 → (可读文本, 结构化 meta)。**原文值一个字不改**，只补列分隔符。"""
    hdr = [str(h or "").strip() for h in (t.header or []) if str(h or "").strip()]
    rows = [
        [str(c or "").strip() for c in r]
        for r in (t.rows or [])
        if any(str(c or "").strip() for c in r)
    ]
    body = [" | ".join(c for c in r if c) for r in rows]
    text = "\n".join([" | ".join(hdr)] + body)
    return text, {"header": hdr, "rows": rows}


def _pos_index(lines: list[Line], y: float, x0: float, page: int | None = None) -> int | None:
    """(y, x0) 在一节的行序列里的位置。**必须带页码**：

    一节的 y 只在页内可比。早先漏了 page，表格的 y=70 会匹配到本节第 259 页顶部的行，
    26 张怪物属性表全挂到第一个条目上（真凶之二）。
    """
    def ok(ln) -> bool:
        return page is None or ln.page == page

    for i, ln in enumerate(lines):
        if ok(ln) and ln.y >= y - 2 and abs(ln.x0 - x0) <= 3:
            return i
    for i, ln in enumerate(lines):
        if ok(ln) and ln.y >= y - 2:
            return i
    return None


def _attach_statblocks(book: Book, blocks: list[Block], stat_tables: list[Table]) -> list[Block]:
    """把属性表并回它所属的条目块，插在**条目名之后、正文之前**。

    归属判据用**阅读顺序位置**，不用 y：双栏页上属性表在左栏，下一页的左栏顶部是
    上一只怪的正文续写，按 y 比大小会把它当成"排在表前面的最近一块"。
    """
    if not blocks:
        return blocks
    page_lines: dict[int, list[Line]] = {}

    def lines_of(pno: int) -> list[Line]:
        if pno not in page_lines:
            page_lines[pno] = book.page(pno).reading_lines()
        return page_lines[pno]

    for t in stat_tables:
        tlines = lines_of(t.page)
        t_idx = _pos_index(tlines, t.y0, t.x0, t.page)
        if t_idx is None:
            continue
        best: tuple[int, Block] | None = None
        for b in blocks:
            if b.page != t.page or not b.lines:
                continue
            if (b.meta or {}).get("list_item") or (b.meta or {}).get("row"):
                continue
            first = b.lines[0]
            bi = _pos_index(lines_of(first.page), first.y, first.x0, first.page)
            if bi is None or bi >= t_idx:
                continue
            if best is None or bi > best[0]:
                best = (bi, b)
        target = best[1] if best else None
        if target is None:
            prev = [
                b for b in blocks
                if b.page == t.page - 1 and b.lines
                and not (b.meta or {}).get("list_item") and not (b.meta or {}).get("row")
            ]
            target = prev[-1] if prev else None
        if target is None:
            continue
        text, sb = _statblock_text(t)
        parts = (target.text or "").split("\n")
        head = parts[0] if parts else ""
        rest = parts[1:]
        target.text = "\n".join([head, text] + rest) if head else "\n".join([text] + rest)
        target.meta = {**(target.meta or {}), "statblock": sb}
        target.page_end = max(target.page_end, t.page)
    return blocks
