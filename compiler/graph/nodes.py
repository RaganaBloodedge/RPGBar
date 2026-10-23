"""图的节点：每个节点一个职责，输入状态、返回增量，不做别的。

设计原则（2026-10-24 重写）：

1. **判定权只归模型。** 切分由 `compiler/segment.py` 让模型读带行号的原文给条目边界；
   归类由 `judge.classify` 让模型判。规则层只做两件事：几何（坐标/阅读顺序/表格/原文偏移）
   与验收（切点合法、按行拼回无损、证据逐字可回指）。**没有词表投票，没有失败回落。**
2. **失败要响。** LLM 拿不到意见就重试、拆批重试，仍失败 → 抛错中止编译。
   一个 62% 由词表判出来的库（上一版就是这样）比一次明确的失败糟糕得多。
3. **节点只吃状态、只吐增量。** 重资产（Book / 连接）走进程内缓存，不进状态。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from .. import db, extract, gate, layout, outline, reader
from .. import selfcheck as selfcheck_mod
from ..pipeline import (
    _chapter_index,
    _log_non_game,
    _report,
    _run_extractors,
    _seed_campaign,
    _write_blocks,
    _write_book,
    _write_flows,
    _write_sections,
    _write_source,
    _write_vectors,
)
from ..reader import Table
from ..schema_skill import SchemaSkill
from ..timeline import Timeline
from .state import CompileState, opt

# ------------------------------------------------------------------ 重资产缓存


@lru_cache(maxsize=2)
def _book_cached(path: str, max_pages: int | None) -> reader.Book:
    return reader.load(path, max_pages=max_pages)


def _book(state: CompileState) -> reader.Book:
    return _book_cached(state["source_path"], state.get("limit_pages"))


_CONNS: dict[str, sqlite3.Connection] = {}


def _conn(out_dir: str, fresh: bool = False) -> sqlite3.Connection:
    """按输出目录单例。`fresh=True` 先关旧连接（写库节点要求干净重来）。"""
    p = str(Path(out_dir) / "kb.sqlite")
    if fresh:
        old = _CONNS.pop(p, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        for suffix in ("", "-wal", "-shm"):
            f = Path(p + suffix)
            if f.exists():
                f.unlink()
    if p not in _CONNS:
        _CONNS[p] = db.connect(p)
    return _CONNS[p]


def reset_caches() -> None:
    """测试/长驻进程用：清掉书与连接缓存（换书后必须清，否则拿到上一本）。"""
    _book_cached.cache_clear()
    for c in list(_CONNS.values()):
        try:
            c.close()
        except Exception:
            pass
    _CONNS.clear()


# ------------------------------------------------------------------ 对象 ↔ 状态


def _flat(b: outline.Block) -> dict:
    """块压成状态友好的 dict。**丢掉 lines**（坐标只在切分时有用）。"""
    return {
        "id": b.id,
        "section_id": b.section_id,
        "order": b.order,
        "page": b.page,
        "page_end": b.page_end,
        "title": b.title,
        "text": b.text,
        "kind": b.kind,
        "subtype": b.subtype,
        "confidence": b.confidence,
        "meta": dict(b.meta or {}),
    }


def _block(d: dict, tables: dict | None = None) -> outline.Block:
    """状态 dict 还原成块。表格按 `block_tables` 转储重建。"""
    dump = (tables or {}).get(d["id"]) or []
    ts = [
        Table(
            page=t.get("page", d.get("page", 0)),
            y0=t.get("y0", 0.0),
            y1=t.get("y1", 0.0),
            header=list(t.get("header") or []),
            rows=[list(r) for r in (t.get("rows") or [])],
            x0=t.get("x0", 0.0),
            x1=t.get("x1", 0.0),
        )
        for t in dump
    ]
    b = outline.Block(
        id=d["id"],
        section_id=d["section_id"],
        order=d.get("order", 0),
        page=d.get("page", 0),
        page_end=d.get("page_end", d.get("page", 0)),
        title=d.get("title") or "",
        text=d.get("text") or "",
        kind=d.get("kind") or "",
        subtype=d.get("subtype") or "",
        confidence=float(d.get("confidence") or 0.0),
        meta=dict(d.get("meta") or {}),
    )
    b.tables = ts
    return b


def _section(d: dict) -> outline.Section:
    return outline.Section(
        id=d["id"],
        level=d.get("level", 1),
        title=d.get("title") or "",
        page_start=d["page_start"],
        page_end=d["page_end"],
        parent_id=d.get("parent_id"),
        order=d.get("order", 0),
        source=d.get("source", "bookmark"),
        anchor_y=d.get("anchor_y"),
        end_page=d.get("end_page"),
        end_y=d.get("end_y"),
        end_title=d.get("end_title") or "",
    )


def _sections(state: CompileState) -> list[outline.Section]:
    return [_section(d) for d in state.get("sections") or []]


def _profile(state: CompileState) -> layout.LayoutProfile:
    return layout.LayoutProfile(**(state.get("layout") or {}))


def _path_of(state: CompileState) -> dict[str, str]:
    return {d["id"]: d.get("path") or d["title"] for d in state.get("sections") or []}


def _chapter_of(state: CompileState) -> dict[str, str]:
    return {d["id"]: d.get("chapter") or "" for d in state.get("sections") or []}


def _tables_dump(b: outline.Block) -> list[dict]:
    return [
        {"page": t.page, "y0": t.y0, "y1": t.y1, "header": list(t.header),
         "rows": [list(r) for r in t.rows], "x0": t.x0, "x1": t.x1}
        for t in (b.tables or [])
    ]


def _warm_schema(schema: SchemaSkill) -> SchemaSkill:
    """字段字典是懒加载的；跨节点重建实例后缓存是空的，报告会读到空表 —— 先预热。"""
    for r in schema.con.execute("SELECT key FROM field").fetchall():
        schema.get(r["key"])
    return schema


def _tr(node: str, t0: float, note: str = "", **metrics) -> dict:
    return {"node": node, "ms": round((time.time() - t0) * 1000), "note": note, "metrics": metrics}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _book_id(path: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "-", Path(path).stem)[:40]


# ------------------------------------------------------------------ ① 读入


def node_read(state: CompileState) -> dict:
    """读 PDF，记页数 / 书签 / 哈希。**源文件缺失也要留 trace**，不能靠异常。"""
    t0 = time.time()
    src = state["source_path"]
    p = Path(src)
    if not p.exists():
        return {"errors": [f"源文件不存在：{src}"], "stage": "read"}
    book = _book(state)
    total = book.page_count
    try:
        import pymupdf

        doc = pymupdf.open(str(p))
        total = doc.page_count
        doc.close()
    except Exception:  # noqa: BLE001
        pass
    chars = sum(len(pg.reading_text()) for pg in book.pages)
    meta = {
        "title": book.title,
        "pages": book.page_count,
        "total_pages": total,
        "bookmarks": len(book.bookmarks or []),
        "bookmark_anchors": len(book.bookmark_anchors or []),
        "sha256": _sha256(str(p)),
        "chars": chars,
    }
    return {
        "book_meta": meta,
        "book_id": state.get("book_id") or _book_id(src),
        "stage": "read",
        "trace": [_tr("read", t0, f"{book.page_count} 页 / 书签 {meta['bookmarks']} 条 / {chars} 字")],
    }


# ------------------------------------------------------------------ ② 版式画像


def node_profile(state: CompileState) -> dict:
    """摸清版式：页眉页脚带、分栏、标题家族。**只测几何，不判内容。**"""
    t0 = time.time()
    book = _book(state)
    prof = layout.profile(book, save_to=Path(state["out_dir"]) / "layout_profile.json")
    return {
        "layout": prof.to_json(),
        "stage": "profile",
        "trace": [_tr("profile", t0, f"类型 {prof.doc_kind} / 主策略 {prof.primary_family or '无'}",
                      confidence=prof.confidence, tables=prof.table_count)],
        "warnings": [f"版式：{w}" for w in prof.warnings],
    }


# ------------------------------------------------------------------ ③ 粗分章节


def node_outline(state: CompileState) -> dict:
    t0 = time.time()
    book = _book(state)

    # 抽样跑（limit_pages）时先裁书签：build_sections 会把越界页码夹回最后一页，
    # 不裁的话全书章节会一起压到前 N 页上，同一批页被重复切分。
    meta = state.get("book_meta") or {}
    partial = int(meta.get("total_pages") or book.page_count) > book.page_count
    src_book = book
    if partial:
        from dataclasses import replace

        src_book = replace(
            book,
            bookmarks=[b for b in (book.bookmarks or []) if 1 <= b[2] <= book.page_count],
            bookmark_anchors=[b for b in (book.bookmark_anchors or []) if 1 <= b[2] <= book.page_count],
        )

    secs = outline.build_sections(src_book, _profile(state), state["book_id"])
    chapter_of, path_of = _chapter_index(secs)

    limit = book.page_count
    kept: list[tuple[outline.Section, int, int]] = []
    for s in secs:
        if s.page_start > limit:
            continue
        start = max(1, min(s.page_start, limit))
        end = max(start, min(s.page_end, limit))
        kept.append((s, start, end))

    out = [
        {
            "id": s.id, "level": s.level, "title": s.title,
            "page_start": start, "page_end": end,
            "parent_id": s.parent_id, "order": s.order, "source": s.source,
            "path": path_of.get(s.id, s.title),
            "chapter": chapter_of.get(s.id, ""),
            # 页内锚点必须跟着走：切块与原文流都靠它把"同页多节"分开
            "anchor_y": s.anchor_y, "end_page": s.end_page,
            "end_y": s.end_y, "end_title": s.end_title,
        }
        for s, start, end in kept
    ]
    return {
        "sections": out,
        "stage": "outline",
        "trace": [_tr("outline", t0, f"{len(out)} 节", sources=_count_by(out, "source"),
                      anchors=sum(1 for s in out if s["anchor_y"] is not None))],
    }


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "")
        out[k] = out.get(k, 0) + 1
    return out


# ------------------------------------------------------------------ ④ 非游戏内容闸门


def node_gate(state: CompileState) -> dict:
    """闸门：版权页/目录/译名表/致谢不进库。**只留页码与理由，不留正文。**"""
    t0 = time.time()
    book = _book(state)
    secs = _sections(state)
    path_of = _path_of(state)
    chapter_of, _ = _chapter_index(secs)

    page_verdict: dict[int, gate.Verdict] = {p.no: gate.judge_page(p) for p in book.pages}
    denied_sections: list[tuple[outline.Section, gate.Verdict]] = []
    kept: list[outline.Section] = []
    for s in secs:
        v = gate.judge_section(s.title)
        if not v.keep:
            denied_sections.append((s, v))
            for pno in s.pages:
                page_verdict[pno] = gate.Verdict(False, v.code, v.detail, v.score)
            continue
        kept.append(s)

    pv_state = {
        str(pno): {"keep": v.keep, "code": v.code, "detail": v.detail, "score": v.score}
        for pno, v in page_verdict.items()
    }
    denied_pages = [{"scope": "page", "page": pno, "code": v.code, "detail": v.detail}
                    for pno, v in page_verdict.items() if not v.keep]
    sec_dicts = [
        {
            "id": s.id, "level": s.level, "title": s.title,
            "page_start": s.page_start, "page_end": s.page_end,
            "parent_id": s.parent_id, "order": s.order, "source": s.source,
            "path": path_of.get(s.id, s.title),
            "chapter": chapter_of.get(s.id, ""),
            "anchor_y": s.anchor_y, "end_page": s.end_page,
            "end_y": s.end_y, "end_title": s.end_title,
        }
        for s in kept
    ]
    return {
        "sections": sec_dicts,
        "page_verdicts": pv_state,
        "denied": {"sections": [{"scope": "section", "page": s.page_start, "title": s.title,
                                 "code": v.code, "detail": v.detail} for s, v in denied_sections],
                   "pages": denied_pages, "blocks": [], "scrubbed": []},
        "stage": "gate",
        "trace": [_tr("gate", t0, f"拒 {len(denied_sections)} 节 / {len(denied_pages)} 页",
                      kept_sections=len(sec_dicts))],
    }


# ------------------------------------------------------------------ ⑤ 细分


def node_split(state: CompileState) -> dict:
    """细分。默认走 LLM：模型读带行号的原文，直接给条目边界。"""
    if opt(state, "use_llm", False) and opt(state, "llm_split", True):
        return _split_by_llm(state)
    return _split_by_layout(state)


def _section_lines(book: reader.Book, sec: outline.Section) -> list[reader.Line]:
    lines: list[reader.Line] = []
    for pno in sec.pages:
        lines.extend(outline.clip_lines(book, sec, pno, book.page(pno).reading_lines()))
    return lines


def _split_by_llm(state: CompileState) -> dict:
    """**模型判条目边界，几何只给原文与验切点。**

    - 输入：这一节的阅读顺序原文，每行带行号（行号就是块边界的坐标）；
    - 判定：模型给"每个条目从哪一行开始、标题是什么"；
    - 验收：行号合法递增、标题来自行首、按行拼回无损、不许逐行切（退化检测）；
    - 表格：一张表落在哪个条目的行区间里就属于哪个条目 —— **区间包含，不是猜**。
    """
    t0 = time.time()
    from .. import segment as seg_mod
    from .llm_classify import get_default_model

    book = _book(state)
    sections = _sections(state)
    path_of = _path_of(state)
    out_dir = Path(state["out_dir"]) / "_seg_cache"
    pv = {int(k): v for k, v in (state.get("page_verdicts") or {}).items()}

    model = get_default_model()
    if model is None:
        raise RuntimeError(
            "llm_split 已启用但拿不到模型端点。切分判定不允许回落规则层 —— "
            "请检查 RPGBAR_LLM 配置，或显式关掉 use_llm 走离线路径。"
        )
    segmenter = seg_mod.Segmenter(model, max_chars=int(opt(state, "llm_split_chars", 2000)))

    blocks: list[dict] = []
    tables_dump: dict[str, list[dict]] = {}
    denied_blocks: list[dict] = []
    scrubbed: list[dict] = []
    stat_n = 0
    self_kinds: list[str] = []

    for sec in sections:
        lines = _section_lines(book, sec)
        if not lines:
            continue
        # 先判这一节是什么，再按那一类的**成员定义**切（4B 对抽象"条目"会退化成逐行切）
        sec_kind = segmenter.classify_section(sec.title, path_of.get(sec.id, ""), lines)
        # 结构证据可以否决语义判断：一节里有 ≥2 张"属性|平均|掷骰"表，那它就是怪物图鉴。
        # 这是**几何事实**（表就在那儿），不是第二个判定者；模型只看开头时会被节首译注带偏。
        n_stat = sum(1 for pno in sec.pages
                     for t in reader.detect_tables(book.page(pno)) if outline._is_statblock_table(t))
        if n_stat >= 2:
            sec_kind = "npc"
        self_kinds.append(sec_kind)
        ents = _cached_segment(segmenter, out_dir, sec, lines, path_of.get(sec.id, ""), sec_kind)

        spans: list[tuple[int, int, outline.Block]] = []
        for k, e in enumerate(ents):
            ls = lines[e["start"]: e["end"]]
            if not ls:
                continue
            b = outline.Block(
                id=f"{sec.id}-e{k:03d}", section_id=sec.id, order=0,
                page=ls[0].page, page_end=ls[-1].page,
                title=(e.get("title") or sec.title)[:40],
                text="\n".join(l.text for l in ls),
                lines=ls,
                meta={"split_by": "llm", "section_kind": sec_kind,
                      "line_span": [e["start"], e["end"]]},
            )
            spans.append((e["start"], e["end"], b))

        # 表格归位：区间包含即归属
        for pno in sec.pages:
            for t in reader.detect_tables(book.page(pno)):
                idx = outline._pos_index(lines, t.y0, t.x0, pno)
                if idx is None:
                    continue
                owner = next((b for s, e, b in spans if s <= idx < e), None)
                if owner is None:
                    continue
                if outline._is_statblock_table(t):
                    text, sb = outline._statblock_text(t)
                    head, _, rest = (owner.text or "").partition("\n")
                    owner.text = f"{head}\n{text}\n{rest}" if head else f"{text}\n{rest}"
                    owner.meta = {**(owner.meta or {}), "statblock": sb}
                    stat_n += 1
                else:
                    owner.tables = list(owner.tables or []) + [t]
                    meta = dict(owner.meta or {})
                    meta["is_table"] = True
                    meta["table_title"] = f"{sec.title}·表"
                    if outline._ITEM_TABLE_RE.search(" ".join(t.header)):
                        meta["item_table"] = True
                    owner.meta = meta

        for _s, _e, b in spans:
            v = pv.get(b.page)
            if v is not None and not v.get("keep", True):
                continue
            bv = gate.judge_block(b.text, b.title)
            if not bv.keep:
                denied_blocks.append({"scope": "block", "page": b.page, "title": b.title,
                                      "code": bv.code, "detail": bv.detail})
                continue
            clean, removed = gate.scrub_text(b.text)
            if clean != b.text:
                scrubbed.extend({"scope": "line", "page": b.page, "title": b.title, "line": ln}
                                for ln in removed)
                if len(clean.strip()) < max(40, len(b.text) * 0.5):
                    denied_blocks.append({"scope": "block", "page": b.page, "title": b.title,
                                          "code": "copyright", "detail": "擦除后剩余内容过少"})
                    continue
                b.text = clean
            if b.tables:
                tables_dump[b.id] = _tables_dump(b)
            b.order = len(blocks)
            blocks.append(_flat(b))

    prev = state.get("denied") or {}
    denied = {
        "sections": list(prev.get("sections") or []),
        "pages": list(prev.get("pages") or []),
        "blocks": denied_blocks,
        "scrubbed": scrubbed,
    }
    avg = round(sum(len(b["text"]) for b in blocks) / max(1, len(blocks)))
    stats = segmenter.stats.to_dict()
    stats["stat_blocks"] = stat_n
    stats["section_kinds"] = _count_by([{"k": k} for k in self_kinds], "k")
    return {
        "blocks": blocks,
        "block_tables": tables_dump,
        "denied": denied,
        "llm_split": stats,
        "stage": "split",
        "trace": [_tr("split", t0, f"{len(blocks)} 条目 · 均值 {avg} 字（LLM 判定）",
                      sections=stats["sections"], windows=stats["windows"],
                      calls=stats["calls"], repairs=stats["repairs"],
                      open_carry=stats["open_carry"], denied_blocks=len(denied_blocks),
                      stat_blocks=stat_n)],
    }


def _split_by_layout(state: CompileState) -> dict:
    """版式启发切块（离线兜底）。只按几何切，不含任何语义判定。"""
    t0 = time.time()
    book = _book(state)
    prof = _profile(state)
    max_chars = int(opt(state, "max_chars", 520))
    pv = {int(k): v for k, v in (state.get("page_verdicts") or {}).items()}

    blocks: list[dict] = []
    tables: dict[str, list[dict]] = {}
    denied_blocks: list[dict] = []
    scrubbed: list[dict] = []

    for sec in _sections(state):
        for b in outline.split_blocks(book, sec, prof, max_chars=max_chars):
            v = pv.get(b.page)
            if v is not None and not v.get("keep", True):
                continue
            bv = gate.judge_block(b.text, b.title)
            if not bv.keep:
                denied_blocks.append({"scope": "block", "page": b.page, "title": b.title,
                                      "code": bv.code, "detail": bv.detail})
                continue
            clean, removed = gate.scrub_text(b.text)
            if clean != b.text:
                scrubbed.extend({"scope": "line", "page": b.page, "title": b.title, "line": ln}
                                for ln in removed)
                if len(clean.strip()) < max(40, len(b.text) * 0.5):
                    denied_blocks.append({"scope": "block", "page": b.page, "title": b.title,
                                          "code": "copyright", "detail": "擦除后剩余内容过少"})
                    continue
                b.text = clean
            if b.tables:
                tables[b.id] = _tables_dump(b)
            blocks.append(_flat(b))

    prev = state.get("denied") or {}
    denied = {
        "sections": list(prev.get("sections") or []),
        "pages": list(prev.get("pages") or []),
        "blocks": denied_blocks,
        "scrubbed": scrubbed,
    }
    avg = round(sum(len(b["text"]) for b in blocks) / max(1, len(blocks)))
    return {
        "blocks": blocks,
        "block_tables": tables,
        "denied": denied,
        "stage": "split",
        "trace": [_tr("split", t0, f"{len(blocks)} 块 · 均值 {avg} 字（版式启发·离线路径）",
                      denied_blocks=len(denied_blocks), scrubbed_lines=len(scrubbed),
                      stat_blocks=sum(1 for b in blocks if (b.get("meta") or {}).get("statblock")))],
    }


# ------------------------------------------------------------------ ⑥ 自检


def node_selfcheck(state: CompileState) -> dict:
    """自检。

    LLM 切分路径下**只统计不修**：`selfcheck.repair` 是按正则重新切/合块，它会把模型判好的
    条目边界改掉（模型说"这是一只怪"，正则说"超过 520 字，切两半"）。边界归模型。
    """
    t0 = time.time()
    max_chars = int(opt(state, "max_chars", 520))
    max_rounds = int(opt(state, "max_rounds", 4))
    blocks = _blocks(state)

    if opt(state, "use_llm", False) and opt(state, "llm_split", True):
        m = selfcheck_mod._metrics(blocks, max_chars)
        return {
            "selfcheck": {"before": m, "after": m, "actions": [], "rounds": 0, "open_defects": [],
                          "done": True, "skipped": "LLM 切分路径：只统计不修"},
            "stage": "selfcheck",
            "trace": [_tr("selfcheck", t0, f"跳过修复 · {m['blocks']} 条目 / 均值 {m['avg_len']} 字",
                          blocks=m["blocks"], avg=m["avg_len"])],
        }

    sc = dict(state.get("selfcheck") or {})
    if not sc:
        sc = {"before": selfcheck_mod._metrics(blocks, max_chars), "actions": [], "rounds": 0}
    rnd = int(state.get("round") or 0)
    defects = selfcheck_mod.audit(blocks, max_chars)
    if defects and rnd < max_rounds:
        fixed, acts = selfcheck_mod.repair(blocks, defects, max_chars)
        rnd += 1
        for a in acts:
            a["round"] = rnd
        sc["actions"] = list(sc.get("actions") or []) + acts
        sc["rounds"] = rnd
        return {"blocks": [_flat(b) for b in fixed], "selfcheck": sc, "round": rnd,
                "stage": "selfcheck",
                "trace": [_tr("selfcheck", t0, f"第 {rnd} 轮：{len(defects)} 缺陷 → 修 {len(acts)} 处")]}
    sc["after"] = selfcheck_mod._metrics(blocks, max_chars)
    sc["open_defects"] = [{"code": d.code, "page": d.page, "title": d.title, "detail": d.detail}
                          for d in selfcheck_mod.audit(blocks, max_chars)]
    sc["done"] = True
    return {"selfcheck": sc, "stage": "selfcheck",
            "trace": [_tr("selfcheck", t0, f"收敛：{sc.get('rounds', 0)} 轮，残留 {len(sc['open_defects'])} 条")]}


def route_selfcheck(state: CompileState) -> str:
    sc = state.get("selfcheck") or {}
    return "classify" if sc.get("done") else "repair"


# ------------------------------------------------------------------ ⑦ 归类


def _blocks(state: CompileState) -> list[outline.Block]:
    tables = state.get("block_tables") or {}
    return [_block(d, tables) for d in (state.get("blocks") or [])]


def node_classify(state: CompileState) -> dict:
    """归类：**模型判，规则不投票。**

    上一版是"规则先判、模型补漏"，实测 2175 块里只有 796 块真问过模型（1351 块是词表判的），
    却对外宣称"LLM 主判"。现在反过来：

    - 每个块都问模型（批量），标签只取自模型；
    - 模型的证据必须能逐字回指原文（标点/空白噪声容忍），否则判废 → unclassified + 争议池；
    - 问到一半拿不到意见 → 逐条重问 → 仍拿不到就**中止编译**。绝不悄悄用词表顶。
    """
    t0 = time.time()
    from .judge import Judge

    blocks = _blocks(state)
    path_of = _path_of(state)
    mode = str(opt(state, "llm_mode", "all" if opt(state, "use_llm", False) else "off"))
    for b in blocks:
        p = path_of.get(b.section_id, "")
        b.meta["chapter"] = p.split(" > ")[0] if p else ""
        b.meta["section"] = p.split(" > ")[-1] if p else ""

    if mode == "off":
        from .. import classify as clf

        classes = {}
        for b in blocks:
            v = clf.classify(b, chapter_title=b.meta.get("chapter", ""),
                             section_title=b.meta.get("section", ""))
            classes[b.id] = {"kind": v.kind, "subtype": v.subtype, "confidence": v.confidence,
                             "evidence": v.evidence, "by": "rule"}
        _write_back(blocks, classes)
        return {"blocks": [_flat(b) for b in blocks], "classes": classes, "llm": {},
                "stage": "classify",
                "trace": [_tr("classify", t0, "离线：规则层归类（仅测试路径）")]}

    judge = Judge(batch=int(opt(state, "llm_batch", 8)), workers=int(opt(state, "llm_workers", 2)),
                  temperature=0.0)
    if not judge.available:
        raise RuntimeError("use_llm 已启用但模型不可用；分类判定不允许回落规则层。")

    verdicts = judge.classify([(b.id, b.title or "", b.text or "", b.meta) for b in blocks])
    missing = [b for b in blocks if b.id not in verdicts]
    if missing:
        j2 = Judge(batch=1, workers=1, temperature=0.0)
        verdicts.update(j2.classify([(b.id, b.title or "", b.text or "", b.meta) for b in missing]))
        missing = [b for b in blocks if b.id not in verdicts]

    classes: dict[str, dict] = {}
    disputed: list[dict] = []
    ok = rejected = 0
    for b in blocks:
        v = verdicts.get(b.id)
        if v is None:
            classes[b.id] = {"kind": "unclassified", "subtype": "", "confidence": 0.0,
                             "evidence": [], "by": "llm"}
            continue
        if v.ok:
            ok += 1
            classes[b.id] = {"kind": v.value["kind"], "subtype": v.value["subtype"],
                             "confidence": v.value["confidence"],
                             "evidence": [v.value["evidence"]],
                             "canonical": v.value.get("canonical", True), "by": "llm"}
        else:
            rejected += 1
            classes[b.id] = {"kind": "unclassified", "subtype": "", "confidence": 0.0,
                             "evidence": [], "by": "llm", "llm_rejected": v.why[:2]}
            disputed.append({
                "block_id": b.id, "title": b.title or "",
                "section": " > ".join(x for x in (b.meta.get("chapter"), b.meta.get("section")) if x),
                "llm_kind": v.value.get("llm_kind") or "",
                "llm_evidence": v.value.get("llm_evidence") or "",
                "why": v.why[:2], "text_head": (b.text or "")[:160],
            })

    if missing:
        raise RuntimeError(
            f"LLM 分类缺 {len(missing)} 块（示例 {[b.id for b in missing[:5]]}）。"
            "分类判定不允许回落规则层，编译中止。"
        )

    # canonical 是**与类别正交**的第二个问题，单独问模型：
    # 混在分类里问，4B 会一律回 true（实测「1.3 游戏范例」整段被当正典）。
    demo = [b for b in blocks if _demo_suspect(b)]
    if demo:
        flags = judge.is_demo([(b.id, b.title or "", b.text or "") for b in demo])
        for b in demo:
            if flags.get(b.id) is True and b.id in classes:
                classes[b.id]["canonical"] = False

    noncanon = _write_back(blocks, classes)
    dist: dict[str, int] = {}
    for c in classes.values():
        dist[c["kind"]] = dist.get(c["kind"], 0) + 1
    return {
        "blocks": [_flat(b) for b in blocks],
        "classes": classes,
        "llm": judge.stats,
        "disputed": disputed,
        "stage": "classify",
        "trace": [_tr("classify", t0,
                      f"{' / '.join(f'{k} {v}' for k, v in sorted(dist.items()))}"
                      f"｜全由模型判：{ok} 块" + (f"｜判废 {rejected}" if rejected else "")
                      + (f"｜非正典 {noncanon}" if noncanon else ""),
                      judged=ok, rejected=rejected, non_canonical=noncanon, missing=0)],
    }


def _write_back(blocks: list[outline.Block], classes: dict[str, dict]) -> int:
    """把判定写回块。返回 canonical=false 的数量。"""
    noncanon = 0
    for b in blocks:
        c = classes.get(b.id) or {}
        b.kind = c.get("kind") or "unclassified"
        b.subtype = c.get("subtype") or ""
        b.confidence = float(c.get("confidence") or 0.0)
        b.meta["verdict"] = {"by": c.get("by"), "evidence": c.get("evidence") or []}
        if c.get("llm_rejected"):
            b.meta["verdict"]["llm_rejected"] = c["llm_rejected"]
        canonical = c.get("canonical", True)
        b.meta["canonical"] = bool(canonical)
        if not canonical:
            noncanon += 1
        if b.kind == "lore" and "summary" not in b.meta:
            b.meta["summary"] = extract.summarize(b.text, 2)
    return noncanon


def node_llm_review(state: CompileState) -> dict:
    """LLM 复核（保留接口）。分类阶段已经问过每一块，这里默认不动。"""
    return {"stage": "llm_review",
            "trace": [_tr("llm_review", time.time(), "跳过：分类阶段已全量问过模型")]}


def route_llm(state: CompileState) -> str:
    """分类之后直接落盘：每一块都已经问过模型，再复核一遍只是重复花钱。"""
    return "store"


# ------------------------------------------------------------------ ⑧ 落盘


def node_store(state: CompileState) -> dict:
    t0 = time.time()
    out = Path(state["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    con = _conn(str(out), fresh=True)
    book = _book(state)
    prof = _profile(state)
    sections = _sections(state)
    blocks = _blocks(state)
    path_of = _path_of(state)
    book_id = state["book_id"]

    _write_book(con, book, prof, book_id, state["source_path"])
    _write_sections(con, sections, book_id, path_of)

    pv = {int(k): gate.Verdict(keep=v["keep"], code=v["code"], detail=v["detail"],
                               score=v.get("score", 0.0))
          for k, v in (state.get("page_verdicts") or {}).items()}
    src = _write_source(con, book, book_id, pv)

    d = state.get("denied") or {}
    scrubbed = [(s.get("page") or 0, s.get("title") or "", s.get("line") or "")
                for s in d.get("scrubbed") or []]
    _log_non_game(con, book_id, [], [], pv, scrubbed=scrubbed)
    _write_blocks(con, blocks, book_id, src["page_text"])
    con.commit()
    return {
        "store": {"kb_path": str(out / "kb.sqlite"), "pages": src["pages"], "chars": src["chars"]},
        "stage": "store",
        "trace": [_tr("store", t0, f"{len(blocks)} 条目 / 原文 {src['pages']} 页 {src['chars']} 字")],
    }


# ------------------------------------------------------------------ ⑨ 抽取


def node_extract(state: CompileState) -> dict:
    t0 = time.time()
    con = _conn(state["out_dir"])
    book = _book(state)
    schema = SchemaSkill(con)
    sections = _sections(state)
    blocks = _blocks(state)
    stats = _run_extractors(con, book, sections, blocks, schema, state["book_id"], _chapter_of(state))
    kind_fixed = schema.refine_kinds()
    con.commit()
    return {
        "extract": {**stats, "kind_fixed": kind_fixed},
        "stage": "extract",
        "trace": [_tr("extract", t0,
                      " / ".join(f"{k} {stats.get(k, 0)}"
                                 for k in ("skill", "item", "npc", "spell", "rule", "lore")),
                      new_fields=stats.get("new_fields"))],
    }


def node_llm_extract(state: CompileState) -> dict:
    """模型补抽（**两段式**）：先问"有哪些实体"，再问"这些实体各有什么字段"。

    为什么拆两段：一上来就让它同时给实体和字段（旧 schema），4B 的输出有近一半是
    截断/非法的 JSON —— 每段都超长，一截断整块就白问。拆开之后每段输出都很短，
    而且第二段把"实体名"直接喂进去，模型只需要抽字段，准确率高得多。
    """
    t0 = time.time()
    if not opt(state, "use_llm", False) or not opt(state, "llm_extract", True):
        return {"stage": "llm_extract", "trace": [_tr("llm_extract", t0, "未启用")]}

    from .judge import Judge

    con = _conn(state["out_dir"])
    schema = _warm_schema(SchemaSkill(con))
    blocks = _blocks(state)
    # 只补"叙述里的人和事"：规则/物品/怪物属性已经有版式抽取器与属性表，不用模型再说一遍
    cand = [
        b for b in blocks
        if b.kind in ("lore", "npc", "pc")
        and not (b.meta or {}).get("statblock")
        and not (b.meta or {}).get("list_item")
        and 40 <= len(b.text or "") <= 1200
    ]
    judge = Judge(batch=int(opt(state, "llm_extract_batch", 6)),
                  workers=int(opt(state, "llm_workers", 2)))
    if not judge.available or not cand:
        return {"stage": "llm_extract", "trace": [_tr("llm_extract", t0, "无模型或无可抽块")]}

    base = [(b.id, b.title or "", b.text or "") for b in cand]
    named = judge.names(base)
    # 第二段：把实体名写进标题一起喂进去
    by_id = {b.id: b for b in cand}
    second = []
    for bid, ents in named.items():
        b = by_id.get(bid)
        if not b:
            continue
        names = "、".join(e["name"] for e in ents[:6])
        second.append((bid, f"{b.title or ''}｜实体：{names}", b.text or ""))
    flds = judge.fields(second) if second else {}

    # 两条**通用**过滤器（不靠词表）：
    #   ① 章节编号式名字（"1.2 合作与竞争"）不是实体；
    #   ② 全书到处都是的名字（"调查员""守秘人"）是角色/称谓，不是可查实体 ——
    #      判据是"它出现在多少块里"，纯数据驱动。
    text_all = [b.text or "" for b in blocks]
    total_blocks = max(1, len(text_all))
    def too_common(name: str) -> bool:
        hit = sum(1 for t in text_all if name in t)
        return hit > max(30, int(total_blocks * 0.03))

    n_ent = n_field = 0
    for bid, ents in named.items():
        b = by_id.get(bid)
        if not b:
            continue
        for e in ents:
            nm = e["name"].strip()
            if re.match(r"^\d+(\.\d+)*\s", nm) or too_common(nm):
                continue
            eid = extract.upsert_entity(con, state["book_id"], e["type"], nm, e["type"],
                                        b.page, block_id=b.id, confidence=0.6)
            n_ent += 1
            for f in flds.get(bid) or []:
                if re.sub(r"\s+", "", f["name"]) != re.sub(r"\s+", "", e["name"]):
                    continue
                fd = schema.observe(e["type"], f["label"], f["value"], origin="llm",
                                    page=b.page, quote=f["quote"])
                extract.add_fact(con, eid, fd, f["value"], f["quote"], b.page, b.id, conf=0.6)
                n_field += 1
    con.commit()
    return {
        "stage": "llm_extract",
        "trace": [_tr("llm_extract", t0,
                      f"问 {len(cand)} 块 → 实体 {n_ent} / 字段 {n_field}"
                      f"（两段式：{len(named)} 块有实体）",
                      asked=len(cand), entities=n_ent, fields=n_field)],
    }


# ------------------------------------------------------------------ ⑩ 图 / 索引 / 报告


def node_graph(state: CompileState) -> dict:
    t0 = time.time()
    con = _conn(state["out_dir"])
    schema = _warm_schema(SchemaSkill(con))
    blocks = _blocks(state)
    stats = _write_flows(con, blocks, schema)
    tl = Timeline(con)
    camp = _seed_campaign(con, tl, state["book_id"], blocks)
    con.commit()
    return {"graph": {**stats, "campaign": camp}, "stage": "graph",
            "trace": [_tr("graph", t0, f"流程 {stats.get('flows', 0)} / 引用 {stats.get('references', 0)}")]}


def node_index(state: CompileState) -> dict:
    t0 = time.time()
    con = _conn(state["out_dir"])
    blocks = _blocks(state)
    info = _write_vectors(con, blocks, bool(state.get("with_vec", True)))
    con.commit()
    return {"index": info, "stage": "index",
            "trace": [_tr("index", t0, f"向量 {info.get('vectors', 0)}" if info.get("enabled")
                          else f"向量关闭（{info.get('reason', '')}）")]}


def node_containers(state: CompileState) -> dict:
    """★ 阶段二：五类容器物化（规则树/技能/物品/NPC/世界观）。

    放在 index 之后：世界观聚类要用 block_vec。判断归模型（子类名/判定步骤/摘要），
    拼装归规则（技能、物品、NPC 档案是从已有的 fact/field 投影出来的）。
    """
    t0 = time.time()
    from .. import containers as containers_mod

    con_path = Path(state["out_dir"]) / "kb.sqlite"
    model = None
    if opt(state, "use_llm", False):
        from .llm_classify import get_default_model

        model = get_default_model()
    stats = containers_mod.build_all(str(con_path), state["book_id"], model=model)
    return {"containers": stats, "stage": "containers",
            "trace": [_tr("containers", t0,
                          f"规则 {stats['rule']['rules']} / 子类 {stats['rule']['subtypes']} / "
                          f"流程 {stats['rule']['flows']} / 技能 {stats['pc']['skills']} / "
                          f"物品 {stats['tool']['items']} / NPC {stats['npc']['npcs']} / "
                          f"世界观 {stats['lore']['topics']}")]}


def node_report(state: CompileState) -> dict:
    t0 = time.time()
    con = _conn(state["out_dir"])
    book = _book(state)
    prof = _profile(state)
    sections = _sections(state)
    blocks = _blocks(state)
    schema = _warm_schema(SchemaSkill(con))
    gate_info = {
        "denied": state.get("denied") or {},
        "page_verdicts": state.get("page_verdicts") or {},
        "denied_pages": sum(1 for v in (state.get("page_verdicts") or {}).values()
                            if not v.get("keep")),
    }
    report = _report(con, book, prof, sections, blocks, schema,
                     state.get("extract") or {}, state.get("graph") or {}, state.get("index") or {},
                     (state.get("graph") or {}).get("campaign") or {}, gate_info)
    report["llm"] = state.get("llm") or {}
    report["containers"] = state.get("containers") or {}
    report["llm_split"] = state.get("llm_split") or {}
    report["disputed"] = {"count": len(state.get("disputed") or []),
                          "items": (state.get("disputed") or [])[:500]}
    out = Path(state["out_dir"])
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    disp = state.get("disputed") or []
    (out / "disputed.json").write_text(
        json.dumps({"count": len(disp), "items": disp}, ensure_ascii=False, indent=1), encoding="utf-8")
    verdict = _verdict(state, report, blocks)
    return {"report": report, "verdict": verdict, "stage": "report",
            "trace": [_tr("report", t0, f"报告已写 · 自证 {'通过' if verdict['passed'] else '未通过'}")]}


def _verdict(state: CompileState, report: dict, blocks: list[outline.Block]) -> dict:
    """这个 Agent 对本次编译的自我判定。**判源覆盖率是硬指标**：模型判不了就不算编译成功。"""
    stats = report.get("stats") or {}
    by_kind = stats.get("by_kind") or {}
    total = max(1, len(blocks))
    llm_judged = sum(1 for b in blocks if (b.meta.get("verdict") or {}).get("by") == "llm")
    coverage = round(llm_judged / total, 4)
    unclassified = by_kind.get("unclassified", 0)
    spec = report.get("spec") or {}
    compliance = spec.get("compliance")
    if compliance is None:
        compliance = 1.0 if not spec.get("violations") else 0.0
    noncanon = sum(1 for b in blocks if (b.meta or {}).get("canonical") is False)
    denied = state.get("denied") or {}
    metrics = {
        "blocks": len(blocks),
        "llm_judged": llm_judged,
        "llm_coverage": coverage,
        "unclassified": unclassified,
        "spec_compliance": compliance,
        "non_canonical": noncanon,
        # 命中缓存时 sections/windows 都是 0，但 calls>0 说明确实走的是 LLM 路径
        "split_mode": "llm" if (state.get("llm_split") or {}).get("calls") else "layout",
        "gate_denied_blocks": len(denied.get("blocks") or []),
        "entities": stats.get("entity"),
        "facts": stats.get("fact"),
    }
    reasons: list[str] = []
    if opt(state, "use_llm", False) and coverage < 0.999:
        reasons.append(f"判源覆盖率 {coverage:.1%} < 100%（有块不是模型判的）")
    if compliance < float(opt(state, "min_compliance", 0.99)):
        reasons.append(f"规范合规率 {compliance:.1%}")
    if unclassified / total > 0.05:
        reasons.append(f"未分类 {unclassified} 块（{unclassified / total:.1%}）")
    return {"passed": not reasons, "reasons": reasons, "metrics": metrics}


_DEMO_CUES = ("例如", "示例", "范例", "举例", "比如", "演示", "实录", "例子", "增补说明")


def _demo_suspect(b: outline.Block) -> bool:
    """这段**可能**是示例吗？只做召回（宁可多问一句），不判定。"""
    title = b.title or ""
    path = (b.meta or {}).get("section") or ""
    text = b.text or ""
    if any(c in path or c in title for c in _DEMO_CUES):
        return True
    # 对话式实录：连续三行以上是"某某：…"
    if len(re.findall(r"^[^\n：:]{1,8}[：:]\s", text, re.M)) >= 3:
        return True
    return any(c in text[:240] for c in _DEMO_CUES)


def _cached_segment(segmenter, cache_dir: Path, sec, lines, path: str, kind: str) -> list[dict]:
    """按节缓存切分结果：**长跑最贵的就是切分**，重跑时已经切好的节不该再问一遍。

    签名 = 本节所有行的文本；改小了窗口/改了提示词不会命中，正好触发重切。
    """
    sig = hashlib.sha1("\n".join(getattr(l, "text", "") for l in lines).encode("utf-8")).hexdigest()[:16]
    f = Path(cache_dir) / f"{sec.id}-{seg_mod_version()}-{sig}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("kind") == kind:
                from ..segment import _merge_fragments

                return _merge_fragments(data["entries"], lines, kind)
        except Exception:  # noqa: BLE001
            pass
    ents = segmenter.segment(sec.title, path, lines, kind)
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"sig": sig, "kind": kind, "entries": ents}, ensure_ascii=False),
                     encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return ents


def seg_mod_version() -> str:
    """缓存版本：切分逻辑一改就变，旧缓存自动不再命中。"""
    from ..segment import SEG_VERSION

    return f"v{SEG_VERSION}"
