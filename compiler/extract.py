"""抽取器：把"某一段原文"变成"实体 + 事实 + 流程 + 摘要"。

这里每个抽取器都遵守同一个约定：

- **输入**是 `Stream`（章节按阅读顺序摊平的原文 + 字符偏移 → 页码映射）与 `SchemaSkill`；
- **边界由内容自己给**：技能边界是"（NN%）"，法术边界是"消耗："，怪物边界是"属性/平均/掷骰"，
  物品边界是表格的一行。**不靠块切得准不准** —— 块切歪了顶多影响检索粒度，不该污染结构化数据；
- **输出**永远带证据：`quote`（原文那一句）+ `page` + `block_id`。

这样"表结构"和"切分质量"解耦：切分是检索问题，抽取是数据问题，各有各的失败模式。
"""
from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from dataclasses import dataclass

from .classify import RULE_DOMAINS
from .schema_skill import SchemaSkill, infer_kind, to_number


# ---------------------------------------------------------------- 章节原文流

@dataclass
class Stream:
    text: str
    page_map: list[tuple[int, int]]  # [(字符偏移, 页码)]

    def page_at(self, off: int) -> int:
        if not self.page_map:
            return 0
        i = bisect_right([p[0] for p in self.page_map], off) - 1
        return self.page_map[max(0, i)][1]

    def lines(self):
        """(行文本, 偏移)。行是抽取的基本单位：标签行、数值行都靠它。"""
        off = 0
        for ln in self.text.split("\n"):
            yield ln, off
            off += len(ln) + 1


def _table_lines(t) -> list[str]:
    """把表摊平成行：表头一行，每行数据一行，单元格用 ` | ` 连接。

    为什么要摊平：属性表（STR/90/5D6×5）在阅读顺序里本来是被拆开的列，摊平后才是
    "STR | 90 | （5D6×5）" 这种一眼能解析的形状。抽取器因此不用关心 PDF 的栏列。
    """
    return [t.header_text] + [" | ".join(r) for r in t.rows]


def build_stream(book, section) -> Stream:
    """章节原文流：按栏读、表格摊平、页码偏移可回溯。

    **区间和切块用同一条规则**（`outline.clip_lines` 的页内锚点）：同一页上挂着好几个
    条目书签时（COC 的 5.6~5.8 全在第 77 页），只按页码取页会让每节都把整页抽一遍，
    结果是同一只怪物被四个章节各抽一次、属性互相覆盖。这里按标题行把不属于本节的行裁掉。
    """
    parts: list[str] = []
    page_map: list[tuple[int, int]] = []
    off = 0
    for pno in section.pages:
        page = book.page(pno)
        out: list[str] = []
        for lo, hi in page.column_bands():
            lines = sorted(
                [l for l in page.body_lines() if lo <= l.x0 < hi], key=lambda l: l.y
            )
            tables = sorted(
                [t for t in page.tables() if lo - 2 <= t.x0 < hi], key=lambda t: t.y0
            )
            ti = 0
            for ln in lines:
                while ti < len(tables) and tables[ti].y0 <= ln.y - 2:
                    out.extend(_table_lines(tables[ti]))
                    ti += 1
                if any(t.y0 - 3 <= ln.y <= t.y1 + 12 for t in tables):
                    continue  # 表格区域内的散行不要（它们已经被摊平成表格行了）
                out.append(ln.text)
            while ti < len(tables):
                out.extend(_table_lines(tables[ti]))
                ti += 1
        out = _clip_page_lines(out, section, pno)
        txt = "\n".join(out)
        if not txt.strip():
            continue
        page_map.append((off, pno))
        parts.append(txt)
        off += len(txt) + 1
    return Stream("\n".join(parts), page_map)


def _clip_page_lines(lines: list[str], section, pno: int) -> list[str]:
    """把一页的行裁到本节区间（与 `outline.split_blocks` 同一套锚点规则）。"""
    if not lines:
        return lines
    from .outline import title_index

    lo, hi = 0, len(lines)
    if pno == getattr(section, "page_start", None):
        i = title_index(lines, getattr(section, "title", ""), getattr(section, "anchor_y", None))
        if i is not None:
            lo = i
    if getattr(section, "end_page", None) == pno:
        j = title_index(lines, getattr(section, "end_title", "") or "",
                        getattr(section, "end_y", None))
        if j is not None:
            hi = max(lo, j)
    if lo == 0 and hi == len(lines):
        return lines
    return lines[lo:hi]


# ---------------------------------------------------------------- 实体写入

def eid(etype: str, name: str) -> str:
    return f"{etype}:{hashlib.sha1(name.encode('utf-8')).hexdigest()[:10]}"


def upsert_entity(
    con, book_id: str, etype: str, name: str, scope: str, page: int,
    block_id: str = "", summary: str = "", aliases: list[str] | None = None,
    confidence: float = 0.7, readonly: int = 1, subtype: str = "",
) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    ident = eid(etype, name)
    row = con.execute("SELECT id FROM entity WHERE id=?", (ident,)).fetchone()
    if row:
        if summary:
            con.execute(
                "UPDATE entity SET summary=COALESCE(NULLIF(summary,''),?) WHERE id=?", (summary, ident)
            )
        if subtype:
            con.execute(
                "UPDATE entity SET subtype=COALESCE(NULLIF(subtype,''),?) WHERE id=?", (subtype, ident)
            )
        return ident
    con.execute(
        """INSERT INTO entity(id,book_id,etype,subtype,name,name_norm,aliases,scope,block_id,page,summary,readonly,confidence)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ident, book_id, etype, subtype, name, name.replace(" ", ""),
         __import__("json").dumps(aliases or [], ensure_ascii=False),
         scope, block_id, page, summary, readonly, confidence),
    )
    return ident


def add_fact(con, entity_id: str, fd, value: str, quote: str, page: int, block_id: str, conf: float = 0.8) -> None:
    if fd is None or not str(value).strip():
        return
    con.execute(
        """INSERT OR IGNORE INTO fact(entity_id,field_key,raw,num,js,unit,quote,block_id,page,confidence)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (entity_id, fd.key, str(value).strip(), to_number(value), None, fd.unit, (quote or "")[:300], block_id, page, conf),
    )


# ---------------------------------------------------------------- 技能

_SKILL_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z·\-—、（）()]{0,18}?)\s*[（(]\s*(?P<base>\d{1,3})\s*%\s*[）)]"
)
_SKILL_CATS = ["格斗", "射击", "科学", "艺术与手艺", "驾驶", "语言", "生存", "博物学", "医学", "机械维修"]


def extract_skills(stream: Stream, sec, schema: SchemaSkill, con, book_id: str, blocks: list) -> list[str]:
    """技能列表：`名称（NN%）` 是这条技能的唯一锚点，描述一直取到下一个锚点。"""
    text = stream.text
    hits = list(_SKILL_RE.finditer(text))
    made: list[str] = []
    for i, m in enumerate(hits):
        name = m.group("name").strip(" ·—、")
        if len(name) < 2 or name in ("技能", "专攻"):
            continue
        base = m.group("base")
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(text), m.end() + 900)
        body = text[m.end() : end].strip("\n ")
        # 描述开头可能是表格行（技能索引页把技能名排成表），跳过纯 "|" 的分隔行
        lines = [ln for ln in body.split("\n") if ln.strip() and ln.strip().strip("|｜ ") != ""]
        body = "\n".join(lines)
        page = stream.page_at(m.start())
        # 描述里的"对抗技能/难度等级："是结构化标签，顺手拆出来
        quoted, desc = _first_sentence(body)
        e = upsert_entity(
            con, book_id, "skill", name, "skill", page,
            block_id=_match_block(blocks, name, page), summary=desc[:160],
            confidence=0.9,
        )
        fd_base = schema.observe("skill", "基础值", f"{base}%", origin="inline", page=page, quote=quoted)
        add_fact(con, e, fd_base, f"{base}%", quoted, page, _match_block(blocks, name, page))
        fd_desc = schema.observe("skill", "描述", desc[:60], origin="inline", page=page, quote=quoted)
        add_fact(con, e, fd_desc, desc, quoted, page, _match_block(blocks, name, page), conf=0.7)
        # 专攻：格斗（斗殴）这种括号里带字的其实是"技能专攻"，单独记一条关系
        for cm in re.finditer(r"对抗技能[:：]?\s*([^\n]{0,40})", body):
            fd_c = schema.observe("skill", "对抗技能", cm.group(1)[:20], origin="label_line", page=page, quote=cm.group(0))
            add_fact(con, e, fd_c, cm.group(1).strip(), cm.group(0), page, "")
        made.append(e)
    return made


def _first_sentence(body: str) -> tuple[str, str]:
    body = re.sub(r"\s*\n\s*", "", body or "")
    m = re.match(r"(.{4,120}?[。！？])", body)
    if m:
        return m.group(1), body
    return body[:80], body


# ---------------------------------------------------------------- 属性 / 角色卡

_ATTR_RE = re.compile(
    r"(力量|体质|体型|敏捷|外貌|智力|意志|教育|幸运)\s*[（(]\s*(STR|CON|SIZ|DEX|APP|INT|POW|EDU|LUCK)\s*[）)]"
)
_DERIVED_RE = re.compile(
    r"^(?P<label>[^\n：:]{1,10})\s*[：:]\s*(?P<value>[^\n]{1,40})$"
)


def extract_attributes(stream: Stream, sec, schema: SchemaSkill, con, book_id: str, blocks: list) -> list[str]:
    """第三章"创建调查员"：把八个基础属性 + 派生属性（HP/MP/SAN/DB/体格/移动）登记成字段。

    注意这里**是"发现"不是"抄清单"**：正则匹配到"力量（STR）"这种写法才建字段。
    换成 DND 的"力量 STR 18"或自定义系统的"灵力（SPIRIT）"，只要版式是同一套，字段照样长出来。
    """
    made: list[str] = []
    text = stream.text
    for m in _ATTR_RE.finditer(text):
        zh, en = m.group(1), m.group(2)
        page = stream.page_at(m.start())
        e = upsert_entity(con, book_id, "attribute", f"{zh}（{en}）", "pc", page,
                          block_id=_match_block(blocks, zh, page), confidence=0.9)
        fd_abbr = schema.observe("pc", "缩写", en, origin="inline", page=page, quote=m.group(0))
        add_fact(con, e, fd_abbr, en, m.group(0), page, "")
        made.append(e)

    # 派生属性：只在"标签：值"且标签像属性时建字段（避免把正文里的冒号全吃进来）
    for ln, off in stream.lines():
        m = _DERIVED_RE.match(ln.strip())
        if not m:
            continue
        label, value = m.group("label").strip(), m.group("value").strip()
        if not re.search(r"(HP|MP|SAN|DB|体格|移动|伤害加成|魔法值|理智|护甲|速度)", label):
            continue
        page = stream.page_at(off)
        schema.observe("pc", label, value, origin="label_line", page=page, quote=ln)
        e = upsert_entity(con, book_id, "attribute", label, "pc", page,
                          block_id=_match_block(blocks, label, page), confidence=0.7)
        fd = schema.get(f"pc.{label}") or schema.observe("pc", label, value, origin="label_line", page=page, quote=ln)
        add_fact(con, e, fd, value, ln, page, "")
    return made


# ---------------------------------------------------------------- NPC / 怪物

_BLOCK_START = re.compile(r"^\s*属\s*性\s*(\||｜|\s)*\s*(平\s*均)?", re.M)
_ATTR_ROW = re.compile(
    r"^\s*(STR|CON|SIZ|DEX|INT|POW|APP|EDU)\s*(?:\||｜|\s)\s*(\d{1,3})\s*(?:\||｜)?\s*([^\n]{0,20})",
    re.M,
)
_HP_ROW = re.compile(r"HP\s*[：:|｜]\s*(\d{1,3})")


def extract_npcs(stream: Stream, sec, schema: SchemaSkill, con, book_id: str, blocks: list) -> list[str]:
    """怪物/人：以"属性/平均/掷骰"三列表头为锚，向上找名字，向下读到下一个锚点。"""
    text = stream.text
    starts = [m.start() for m in _BLOCK_START.finditer(text)]
    made: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(text), start + 1600)
        chunk = text[start:end]
        page = stream.page_at(start)

        name_raw = _name_before(text, start, sec.title, blocks, page)
        name, aliases = _split_name(name_raw)
        if not name or "|" in name or "｜" in name:
            continue
        # 名字后面紧跟"属性"表 → 这是一只怪物/神话生物；否则跳过（比如"属性"出现在正文里）
        rows = {m.group(1): (m.group(2), (m.group(3) or "").strip()) for m in _ATTR_ROW.finditer(chunk)}
        if len(rows) < 3:
            continue
        etype = "monster" if re.search(r"(怪物|神话生物|邪神|外神|野兽)", sec.title + name) else "npc"
        type_scope = "monster"
        e = upsert_entity(con, book_id, etype, name, type_scope, page,
                          block_id=_match_block(blocks, name, page), confidence=0.85,
                          aliases=aliases)
        schema.register_etype(etype, type_scope, page, f"{name} 的属性表")
        bid = _match_block(blocks, name, page)

        for abbr, (num, dice) in rows.items():
            # 字段就是属性名本身（npc.STR），不是一列通用的"平均值" ——
            # 否则卡片上会出现六行"平均值 = 90"，谁也不知道哪行是力量
            fd_num = schema.observe(type_scope, abbr, num, origin="table_header", page=page,
                                    quote=f"{abbr} {num} {dice}")
            add_fact(con, e, fd_num, num, f"{abbr} {num} {dice}", page, bid)
            if dice:
                fd_dice = schema.observe(type_scope, f"{abbr}·掷骰", dice, origin="table_header",
                                         page=page, quote=f"{abbr} {num} {dice}")
                add_fact(con, e, fd_dice, dice, f"{abbr} {num} {dice}", page, bid)

        # 表头之后的所有"标签：值"行都是这只怪物的字段（HP / 护甲 / 移动 / 特殊能力…）
        for ln, off in _lines_in_range(text, start, end):
            m = _DERIVED_RE.match(ln.strip())
            if m:
                label, value = m.group("label").strip(), m.group("value").strip()
                if len(label) <= 10 and value:
                    fd = schema.observe(type_scope, label, value, origin="label_line",
                                        page=stream.page_at(off), quote=ln)
                    add_fact(con, e, fd, value, ln, stream.page_at(off), _match_block(blocks, name, page))
            elif "：" in ln or ":" in ln:
                label, _, value = re.split(r"[：:]", ln, maxsplit=1)[0], ":", re.split(r"[：:]", ln, maxsplit=1)[1]
                label, value = label.strip(), value.strip()
                if 1 <= len(label) <= 10 and value:
                    fd = schema.observe(type_scope, label, value, origin="inline",
                                        page=stream.page_at(off), quote=ln)
                    add_fact(con, e, fd, value, ln, stream.page_at(off), _match_block(blocks, name, page))
        made.append(e)
    return made


_STRUCT_WORDS = {
    "特殊能力", "攻击", "属性", "移动", "护甲", "法术", "技能", "防御", "战斗方式",
    "每回合攻击", "理智损失", "栖息地", "简介", "描述",
}
_ABBR = {"STR", "CON", "SIZ", "DEX", "INT", "POW", "APP", "EDU", "LUK", "LUCK"}
_COLON_RE = re.compile(r"[：:]")
_LABEL_TAIL = re.compile(r"[\u4e00-\u9fa5A-Za-z]")


def _label_pairs(line: str) -> list[tuple[str, str]]:
    """把一行摊平文本里的所有 `标签：值` 拆出来（"HP：13伤害加成：1D4移动：8" → 三对）。

    为什么不能按行整体取值：PDF 的怪物档案里 HP / 伤害加成 / 体格 / 魔法值 / 移动 常被
    排成同一行，整行当一个值会让卡片出现"HP = 13伤害加成：1D4…"这种鬼东西。
    标签取冒号前连续的汉字/字母（遇数字停），**下一个标签的起点就是本值的终点**。
    """
    cols = [m.start() for m in _COLON_RE.finditer(line or "")]
    out: list[tuple[str, str]] = []
    for k, pos in enumerate(cols):
        start = pos
        while start > 0 and pos - start < 8 and _LABEL_TAIL.match(line[start - 1]):
            start -= 1
        label = line[start:pos].strip()
        if not (2 <= len(label) <= 8):
            continue
        if k + 1 < len(cols):
            p2 = cols[k + 1]
            s2 = p2
            while s2 > 0 and p2 - s2 < 8 and _LABEL_TAIL.match(line[s2 - 1]):
                s2 -= 1
        else:
            s2 = len(line)
        value = line[pos + 1 : s2].strip(" \t。，,；;：:")
        if value:
            out.append((label, value[:60]))
    return out


def extract_npcs_from_blocks(blocks: list, schema: SchemaSkill, con, book_id: str) -> list[str]:
    """怪物/人：**一块一份档案**（属性表已在切块阶段并回条目块）。

    与 `extract_npcs`（按原文流的表头锚点切块）的分工：

    - 块带 `meta.statblock` → 用块。字段归属由**块边界**保证，物理上不可能串到下一只怪；
    - 不带 → 退回流式抽取（老版式、属性表没被识别出来的书）。

    这一条直接对应 handoff §4.1 的"深潜者 CON=755"：原来抽取器自己在整节原文里找
    "属性|平均|掷骰"表头当锚点，一只怪的档案有多长全靠猜；表头漏检一次，下一只怪的属性
    就并进上一只。现在锚点是**块**，块有多大档案就有多大。
    """
    made: list[str] = []
    by_sec: dict[str, list] = {}
    for b in blocks:
        by_sec.setdefault(b.section_id, []).append(b)
    for b in blocks:
        sb = (b.meta or {}).get("statblock")
        if not sb:
            continue
        name_raw = (b.title or "").strip()
        name, aliases = _split_name(name_raw)
        if not _valid_name(name):
            # 属性表落在了一个碎片块上（"护甲：…"、"属性"）：**往前找最近一个合法怪物名**。
            # 这是几何意义上的"最近邻"，不是猜语义；找不到就不建实体（宁缺勿错）。
            sib = by_sec.get(b.section_id) or []
            k = sib.index(b) if b in sib else -1
            sec_title = ((b.meta or {}).get("section") or "").strip()
            for j in range(k - 1, max(-1, k - 8), -1):
                cand, al = _split_name((sib[j].title or "").strip())
                if not _valid_name(cand):
                    continue
                # 节标题不是怪物名（"14.1 神话生物"被当成一只怪的问题）
                if sec_title and cand == sec_title:
                    continue
                name, aliases = cand, al
                break
            else:
                continue
        text = b.text or ""
        ctx = f"{(b.meta or {}).get('chapter') or ''} {(b.meta or {}).get('section') or ''}"
        # 怪物 vs 人：判据是"是不是可战斗对象"（handoff 决策 5），不是词表命中
        combat = any(w in text for w in ("攻击", "护甲", "理智丧失", "理智损失", "闪避", "格斗"))
        etype = "monster" if (re.search(r"(怪物|神话生物|邪神|妖魔|野兽)", ctx) or combat) else "npc"
        e = upsert_entity(con, book_id, etype, name, "monster", b.page, block_id=b.id,
                          confidence=0.85, aliases=aliases)
        schema.register_etype(etype, "monster", b.page, f"{name} 的属性表")
        for row in sb.get("rows") or []:
            cells = [str(c or "").strip() for c in row]
            if not cells:
                continue
            abbr = cells[0].upper() if cells[0].upper() in _ABBR else cells[0]
            num = cells[1] if len(cells) > 1 else ""
            dice = cells[2] if len(cells) > 2 else ""
            if not cells[0] or not (num or dice):
                continue
            quote = " ".join(x for x in (cells[0], num, dice) if x)
            if num:
                fd = schema.observe("monster", abbr, num, origin="table_header",
                                    page=b.page, quote=quote)
                add_fact(con, e, fd, num, quote, b.page, b.id)
            if dice:
                fd_d = schema.observe("monster", f"{abbr}·掷骰", dice, origin="table_header",
                                      page=b.page, quote=quote)
                add_fact(con, e, fd_d, dice, quote, b.page, b.id)
        for ln in text.split("\n"):
            for label, value in _label_pairs(ln):
                fd = schema.observe("monster", label, value, origin="label_line",
                                    page=b.page, quote=ln)
                add_fact(con, e, fd, value, ln, b.page, b.id)
        made.append(e)
    return made
_FUNC_CHARS = "就它也这那其并且但而则便很的以可"


def _split_name(s: str) -> tuple[str, list[str]]:
    """把条目名拆成「主名 + 别名」。

    COC 的怪物条目几乎都写成「拜亚基，星骏」这种"主名，别称"格式。玩家嘴上是"拜亚基"，
    不是全名 —— 所以主名取逗号前那段，逗号后的当别名一起进检索索引。
    """
    s = (s or "").strip()
    if "，" in s:
        head, _, tail = s.partition("，")
        if 2 <= len(head) <= 8 and tail.strip():
            return head.strip(), [x.strip() for x in tail.split("，") if x.strip()]
    return s, []


def _valid_name(s: str) -> bool:
    """实体名合法性：宁可漏，不可错。

    实测被错认成名字的有：「——H．P．洛夫克拉夫特，《魔宴》」（引文出处）、
    「属性 | 平均 | 掷骰」（表头）、「质，它一伸一缩，就」（跨页续行）、
    「移动：12」（字段行）。这些进了知识库，DM 查"怪物"时会拿到一张叫"属性"的卡。
    """
    s = (s or "").strip()
    if not (2 <= len(s) <= 24):
        return False
    if s in _STRUCT_WORDS:
        return False
    if any(ch in s for ch in "。！？；：、…《》“”()（）|｜"):
        return False
    if re.search(r"\d", s):  # 名字里基本不会有数字，有的话是数值行
        return False
    if s.startswith("—") or s.startswith("-"):
        return False
    if any(ch in s for ch in _FUNC_CHARS):  # 句子里的虚词
        return False
    if "，" in s:  # 允许"主名，别称"，但主名本身必须短
        head = s.split("，")[0]
        if not (2 <= len(head) <= 8):
            return False
    return True


def _name_before(text: str, pos: int, fallback: str, blocks: list = None, page: int = 0) -> str:
    """锚点往上找实体名。**优先用块标题交叉验证**，纯文本猜测只做兜底。

    为什么不只靠"往上找第一个短行"：跨页的时候，页首那几行是上一页句子的续行，
    实测会造出叫「质，它一伸一缩，就」的怪物。块切分已经把条目头识别成了块标题，
    这里只要在附近找到那个标题就对上了 —— 两条独立线索互相校验，比单条调参可靠。
    """
    if blocks:
        near = sorted(
            [b for b in blocks if (b.title or "") and abs((b.page or 0) - page) <= 1],
            key=lambda b: b.page,
        )
        for b in reversed(near):
            if b.title in text[max(0, pos - 500) : pos] and _valid_name(b.title):
                return b.title.strip()

    head = text[max(0, pos - 400) : pos]
    for ln in reversed([x.strip() for x in head.split("\n") if x.strip()]):
        if len(ln) > 24:
            return ""
        if _valid_name(ln):
            return ln
    return ""


def _lines_in_range(text: str, start: int, end: int):
    off = start
    for ln in text[start:end].split("\n"):
        yield ln, off
        off += len(ln) + 1


# ---------------------------------------------------------------- 物品（表格驱动）

def extract_items_from_table(block, schema: SchemaSkill, con, book_id: str) -> list[str]:
    """表格行 → 物品。**列名就是字段名**，不需要预先知道"武器表有 8 列"。

    切分阶段已经把大表按行拆成块（一块一行），这里同时兼容两种形态：
    - `meta.row/header`（新，一行一块）：直接落一个物品；
    - `block.tables`（旧，小表整块）：逐行落物品。
    """
    made: list[str] = []
    scope = "item"

    header = block.meta.get("header") if block.meta else None
    row = block.meta.get("row") if block.meta else None
    if header and row and len(header) >= 2:
        name = (row[0] or "").strip()
        if name and len(name) <= 40:
            fds = schema.observe_headers(scope, header[1:], values=row[1:], page=block.page, block_id=block.id)
            e = upsert_entity(con, book_id, "item", name, scope, block.page,
                              block_id=block.id, confidence=0.85, subtype=block.subtype)
            for j, fd in enumerate(fds):
                v = row[j + 1] if j + 1 < len(row) else ""
                add_fact(con, e, fd, v, " | ".join(row), block.page, block.id)
            made.append(e)
        return made

    for t in block.tables:
        if len(t.header) < 2:
            continue
        fds = schema.observe_headers(scope, t.header[1:], values=(t.rows[0][1:] if t.rows else None),
                                     page=t.page, block_id=block.id)
        for r in t.rows:
            if len(r) < 2:
                continue
            name = r[0].strip()
            if not name or len(name) > 40:
                continue
            e = upsert_entity(con, book_id, "item", name, scope, t.page,
                              block_id=block.id, confidence=0.85, subtype=block.subtype)
            for j, fd in enumerate(fds):
                v = r[j + 1] if j + 1 < len(r) else ""
                add_fact(con, e, fd, v, " | ".join(r), t.page, block.id)
            made.append(e)
    return made


# ---------------------------------------------------------------- 法术

_SPELL_HINT = ("消耗", "施法用时", "别名", "理智", "POW")


def extract_spells(stream: Stream, sec, schema: SchemaSkill, con, book_id: str, blocks: list) -> list[str]:
    """法术：一个短行 + 紧随其后的"消耗：/施法用时："就是一条法术。"""
    lines = list(stream.lines())
    made: list[str] = []
    for i, (ln, off) in enumerate(lines):
        t = ln.strip()
        if not (2 <= len(t) <= 20) or t[-1] in "。！？：:，,；;":
            continue
        window = "\n".join(x for x, _ in lines[i + 1 : i + 6])
        if not any(h in window for h in _SPELL_HINT):
            continue
        page = stream.page_at(off)
        e = upsert_entity(con, book_id, "spell", t, "spell", page,
                          block_id=_match_block(blocks, t, page), confidence=0.75)
        schema.register_etype("spell", "spell", page, f"{t}：消耗/施法用时")
        made.append(e)
        for ln2, off2 in lines[i + 1 : i + 8]:
            m = _DERIVED_RE.match(ln2.strip())
            if not m:
                continue
            label, value = m.group("label").strip(), m.group("value").strip()
            if len(label) > 8:
                continue
            fd = schema.observe("spell", label, value, origin="label_line",
                                page=stream.page_at(off2), quote=ln2)
            add_fact(con, e, fd, value, ln2, page, _match_block(blocks, t, page))
    return made


# ---------------------------------------------------------------- 规则流程图

_STEP_RE = re.compile(r"^\s*(?:\d{1,2}[.、)]|步骤\s*\d|第[一二三四五六七八九十]+步)\s*")
_BRANCH_RE = re.compile(r"(如果|若|当)[^。；\n]{0,40}(则|就|那么|便)|(否则|不然)")


def extract_rule_flow(block, schema: SchemaSkill, con) -> dict | None:
    """规则 → 流程图。

    抽法很朴素，但**可解释**：编号步骤是顺序节点；每步里出现"如果…则…"就再挂一个判定
    节点和两条出口（成立/不成立）。抽不出的规则返回 None，而不是硬编一个假流程 ——
    DM 拿到假流程比拿不到更危险。
    """
    lines = [l for l in (block.text or "").split("\n") if l.strip()]
    steps = [l.strip() for l in lines if _STEP_RE.match(l)]
    if len(steps) < 2:
        return None
    nodes, edges = [], []
    for i, s in enumerate(steps):
        nodes.append({"id": f"n{i}", "type": "step", "text": _STEP_RE.sub("", s)[:120], "page": block.page})
        if i:
            edges.append({"from": f"n{i-1}", "to": f"n{i}", "label": ""})
        m = _BRANCH_RE.search(s)
        if m:
            cond = re.sub(r"^.*?(如果|若|当)", "", s)[:60]
            nodes.append({"id": f"d{i}", "type": "decision", "text": cond, "page": block.page})
            edges.append({"from": f"n{i}", "to": f"d{i}", "label": "判定"})
            edges.append({"from": f"d{i}", "to": f"n{i}", "label": "成立"})
            edges.append({"from": f"d{i}", "to": "end", "label": "不成立"})
    nodes.append({"id": "end", "type": "end", "text": "结束", "page": block.page})
    return {"kind": "decision" if any(n["type"] == "decision" for n in nodes) else "sequence",
            "nodes": nodes, "edges": edges}


# ---------------------------------------------------------------- 常识与世界观

_SENT_SPLIT = re.compile(r"(?<=[。！？；])")


def summarize(text: str, max_sent: int = 3) -> str:
    """抽取式摘要（不需要模型）：按"词频 × 位置"给句子打分。

    为什么先做抽取式：这一步每次编译都要跑几千次，小模型跑不动；而摘要的作用只是建索引、
    给 DM 一个"这段大概讲什么"的钩子，真正的答案永远回原文。需要更好摘要时把 llm 钩子打开即可。
    """
    sents = [s.strip() for s in _SENT_SPLIT.split(re.sub(r"\s+", "", text or "")) if len(s.strip()) > 8]
    if len(sents) <= max_sent:
        return "".join(sents)
    freq: dict[str, int] = {}
    for s in sents:
        for w in re.findall(r"[\u4e00-\u9fa5]{2}", s):
            freq[w] = freq.get(w, 0) + 1
    scored = []
    for i, s in enumerate(sents):
        base = sum(freq.get(w, 0) for w in re.findall(r"[\u4e00-\u9fa5]{2}", s)) / max(1, len(s))
        scored.append((base + (0.6 if i == 0 else 0) + (0.3 if i == 1 else 0), i, s))
    picked = sorted(scored, key=lambda x: -x[0])[:max_sent]
    return "".join(s for _, _, s in sorted(picked, key=lambda x: x[1]))


# 前言/目录这类"书的零件"不是世界观。它们进了 lore 桶，DM 查"神话"时会先撞到版权声明。
_FRONT_MATTER = re.compile(r"^(目录|使用说明|前言|译名表|更新历史|中文化进度表|附录|空白页)")
_PURE_LATIN = re.compile(r"^[\d\sA-Za-z.·:：\-—]+$")

_LORE_SUBTYPE_BY_CHAPTER = [
    ("爱手艺", "神话"),
    ("神话", "神话"),
    ("典籍", "典籍"),
    ("法术", "神话"),
    ("主持游戏", "叙事技巧"),
    ("模组", "剧情"),
    ("介绍", "世界观"),
]


def _lore_ok(title: str) -> bool:
    t = (title or "").strip()
    if not (2 <= len(t) <= 24):
        return False
    if _FRONT_MATTER.match(t) or re.match(r"^\d+\s*版", t):
        return False
    if t.startswith("——"):
        return False
    if _PURE_LATIN.match(t):               # 纯英文/数字（页眉、编号、译名）
        return False
    # 标签行/署名行/被截断的书名都不是 topic：
    #   "理智损失：1D6"（标签）· "译/七宫涟"（署名）· "波恩之书》"（书名被切了一半）
    if any(ch in t for ch in "：:，、/|｜"):
        return False
    if t.count("《") != t.count("》"):
        return False
    return True


def _lore_subtype(chapter: str, title: str) -> str:
    for kw, sub in _LORE_SUBTYPE_BY_CHAPTER:
        if kw in (chapter or ""):
            return sub
    for kw, sub in (("历史", "历史"), ("地点", "地点"), ("人物", "人物"), ("神", "神话")):
        if kw in (title or ""):
            return sub
    return "世界观"


def extract_lore_blocks(
    blocks: list, schema: SchemaSkill, con, book_id: str, chapter_of: dict | None = None
) -> list[str]:
    """常识与世界观：每块一个 topic，**摘要 + 原文**；语义相近的合并到同一个 topic 下。

    合并规则（不用向量也能跑）：
    - 标题归一后相同/包含 → 同一 topic；
    - **正文 vs 正文**的字面 trigram 相似度 > 0.55 → 同一 topic。
      （早先拿"正文 vs 摘要"比，量纲不一致，永远比不中，结果同一段话被存成三个 topic。）
    合并时不覆盖，新的原文作为 `补充原文` 追加一条 fact —— 需求里说的"在摘要下面补充原文"。
    """
    made: list[str] = []
    seen: dict[str, tuple[str, str]] = {}  # 归一标题 -> (实体 id, 首段原文)
    for b in blocks:
        title = (b.title or "").strip()
        if not _lore_ok(title):
            continue
        norm = re.sub(r"[\s\d.、：:·（）()]", "", title)
        target = None
        for k, (ident, first_text) in seen.items():
            if norm and (norm in k or k in norm) and abs(len(norm) - len(k)) <= 4:
                target = ident
                break
            if _trigram_sim(b.text[:400], first_text[:400]) > 0.55:
                target = ident
                break

        summary = b.meta.get("summary") or summarize(b.text, 2)
        b.meta["summary"] = summary
        chapter = (chapter_of or {}).get(b.section_id, "")
        sub = _lore_subtype(chapter, title)

        if target is None:
            ident = upsert_entity(
                con, book_id, "lore", title, "lore", b.page, block_id=b.id,
                summary=summary, confidence=0.6, subtype=sub,
            )
            fd_main = schema.observe("lore", "原文", b.text[:40], origin="inline", page=b.page, quote=title)
            add_fact(con, ident, fd_main, b.text[:1200], title, b.page, b.id, conf=0.9)
            fd_sum = schema.observe("lore", "摘要", summary[:40], origin="inline", page=b.page, quote=title)
            add_fact(con, ident, fd_sum, summary, title, b.page, b.id)
            seen[norm or title] = (ident, b.text)
            made.append(ident)
        else:
            fd_add = schema.observe("lore", "补充原文", b.text[:40], origin="inline", page=b.page, quote=title)
            add_fact(con, target, fd_add, b.text[:1200], title, b.page, b.id, conf=0.85)
    return made


def _trigram_sim(a: str, b: str) -> float:
    def grams(s: str) -> set[str]:
        s = re.sub(r"[\s，。！？；：、,.!?;:]+", "", s or "")
        return {s[i : i + 3] for i in range(0, max(0, len(s) - 2))}

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# ---------------------------------------------------------------- 清单型物品（价格表）

def extract_price_items(blocks: list, schema: SchemaSkill, con, book_id: str) -> list[str]:
    """「名称，价格」清单 → 一个商品一个实体。

    为什么这条要单独写：这类内容既不是表格（没有列），也不是段落（一行一条），
    它只有"名字 + 价格"两个槽。切分阶段已经把它一行一块切好了，这里只负责落库。
    """
    made: list[str] = []
    for b in blocks:
        meta = b.meta or {}
        if not meta.get("list_item"):
            continue
        name, price = meta.get("name"), meta.get("price")
        if not name or not price:
            continue
        group = meta.get("group") or ""
        e = upsert_entity(con, book_id, "item", name, "item", b.page, block_id=b.id,
                          confidence=0.9, subtype=group or "价格")
        fd_price = schema.observe("item", "价格", price, origin="inline", page=b.page, quote=b.text)
        add_fact(con, e, fd_price, price, b.text, b.page, b.id)
        if group:
            fd_g = schema.observe("item", "分组", group, origin="inline", page=b.page, quote=b.text)
            add_fact(con, e, fd_g, group, b.text, b.page, b.id)
        made.append(e)
    return made


# ---------------------------------------------------------------- 块 ↔ 实体 连接

def _match_block(blocks: list, name: str, page: int) -> str:
    """给实体找"它的原文块"：同页 + 标题含名字优先。"""
    for b in blocks:
        if name and name in (b.title or "") and abs(b.page - page) <= 1:
            return b.id
    for b in blocks:
        if b.page == page:
            return b.id
    return blocks[0].id if blocks else ""
