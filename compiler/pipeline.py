"""编排：把上面各层串成一个**可复跑、可审计、可自证**的编译流程。

九个阶段（每个阶段的产出都能单独落盘/单测）：

```
S0 读入      PDF → 页/行/词
S1 画像      ★ 摸清版式（证据→假设→打分→定策），产出 layout_profile.json
S2 粗分      章节树（书签/目录页/版式标题），带页码范围与父子路径
S3 细分      块（按段落边界 + 条目头，不按页硬切）
S4 归类      五大类 + 子类（子类从内容里长）
S5 抽取      技能/属性/物品/法术/NPC/规则/世界观，字段走 schema_skill 自动拓展
S6 建图      规则流程 + 交叉引用 + 时间线场景
S7 索引      FTS5(BM25) + 向量
S8 自检      覆盖率、可疑块、字段审计、报告
```

为什么强调"可复跑"：编译是有主观判断的（哪个是标题、哪块算规则）。判断必须能被质疑，
所以每个结论都要留证据（页码、原文、打分），重跑一遍结果要一致（同样的输入 → 同样的输出），
否则调参就成了玄学。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import classify as clf
from . import db, extract, gate, layout, outline, reader
from . import selfcheck as selfcheck_mod
from . import spec as spec_mod
from .schema_skill import SchemaSkill
from .timeline import Timeline, extract_year

COMPILER_VERSION = "0.1.0"


@dataclass
class Ctx:
    book: reader.Book
    prof: layout.LayoutProfile
    book_id: str
    sections: list[outline.Section] = field(default_factory=list)
    blocks: list[outline.Block] = field(default_factory=list)
    sec_by_id: dict = field(default_factory=dict)
    chapter_of: dict = field(default_factory=dict)  # section_id -> 一级章标题
    path_of: dict = field(default_factory=dict)
    chapters: list[str] = field(default_factory=list)  # 书里都有哪些一级章


def _chapter_index(sections: list[outline.Section]) -> tuple[dict, dict]:
    """每个 section 归属哪一章 + 它的完整路径（DM 引用出处时要有"第六章 > 6.2"这种路径）。"""
    chapter_of: dict[str, str] = {}
    path_of: dict[str, str] = {}
    by_id = {s.id: s for s in sections}
    for s in sections:
        chain, cur = [], s
        seen = set()
        while cur and cur.id not in seen:
            seen.add(cur.id)
            chain.append(cur.title)
            cur = by_id.get(cur.parent_id)
        path_of[s.id] = " > ".join(reversed(chain))
        # 最近的一级标题
        top = next((c for c in reversed(chain) if by_id.get(s.id) and c and _is_chapter(c)), "")
        chapter_of[s.id] = top or (chain[-1] if chain else "")
    return chapter_of, path_of


def _is_chapter(title: str) -> bool:
    return bool(re.match(r"^(第[〇零一二三四五六七八九十百千0-9]+[章篇部]|附录|译名表)", title or ""))


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compile_book(
    pdf_path: str,
    out_dir: str,
    book_id: str | None = None,
    limit_pages: int | None = None,
    with_vec: bool = True,
) -> dict:
    t_all = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}

    # ---------------- S0 读入
    t = time.time()
    book = reader.load(pdf_path, max_pages=limit_pages)
    timings["S0_read"] = round(time.time() - t, 2)

    # ---------------- S1 画像
    t = time.time()
    prof = layout.profile(book, save_to=out / "layout_profile.json")
    timings["S1_profile"] = round(time.time() - t, 2)
    book_id = book_id or re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "-", Path(pdf_path).stem)[:40]

    # ---------------- S2 粗分
    t = time.time()
    sections_all = outline.build_sections(book, prof, book_id)
    timings["S2_sections"] = round(time.time() - t, 2)

    # ---------------- S2.5 闸门（非游戏内容不进知识层）
    # 三条硬机制之一：**结构层先拒**（目录/使用说明/版权/译者这些靠书签就能定位），
    # 再对每一页跑内容判定。判不准就拒 —— 宁可少一条世界观，也不要版权声明混进检索。
    t = time.time()
    denied_sections: list[tuple[outline.Section, gate.Verdict]] = []
    sections: list[outline.Section] = []
    for sec in sections_all:
        v = gate.judge_section(sec.title)
        if v.keep:
            sections.append(sec)
        else:
            denied_sections.append((sec, v))
    chapter_of, path_of = _chapter_index(sections)

    page_verdict: dict[int, gate.Verdict] = {}
    for p in book.pages:
        page_verdict[p.no] = gate.judge_page(p)
    timings["S2_gate"] = round(time.time() - t, 2)

    # ---------------- S3 细分 + S3.5 自检修复 + S4 归类
    t = time.time()
    blocks: list[outline.Block] = []
    denied_blocks: list[tuple[outline.Block, gate.Verdict]] = []
    scrubbed_lines: list[tuple[int, str, str]] = []  # 行级擦除记录（页码/标题/被删的行）
    MAX_CHARS = 520
    sec_by_id = {s.id: s for s in sections}
    for sec in sections:
        for b in outline.split_blocks(book, sec, prof, max_chars=MAX_CHARS):
            # 页被拒 / 块内容像版权或名单 → 整块不进知识层
            pv = page_verdict.get(b.page)
            if pv is not None and not pv.keep:
                continue  # 整页已被拒，页级日志已经记过，不必逐块重复记
            bv = gate.judge_block(b.text, b.title)
            if not bv.keep:
                denied_blocks.append((b, bv))
                continue
            # 行级擦除：署名行/版权行从块里删掉（整页拒绝会把章首页的正文一起毙了）
            clean, removed = gate.scrub_text(b.text)
            if clean != b.text:
                scrubbed_lines.extend((b.page, b.title, ln) for ln in removed)
                if len(clean.strip()) < max(40, len(b.text) * 0.5):
                    denied_blocks.append((b, gate.Verdict(False, "copyright", "擦除后剩余内容过少", 0.9)))
                    continue
                b.text = clean
            blocks.append(b)

    # S3.5 自检 + 自动重切：Agent 自己判断"切干净了没有"，不合格就修，最多 3 轮
    blocks, selfcheck = selfcheck_mod.run(blocks, MAX_CHARS, max_rounds=4)

    # S4 归类（在自检修出来的新块上也要跑一遍，所以放在自检之后）
    # 「（续N）」这类续块继承父条目的类别 —— 拆开之后它自身可能没有判定特征
    by_base = {}
    for b in blocks:
        base = re.sub(r"（续\d+）$", "", b.title or "")
        if base and base != (b.title or ""):
            by_base.setdefault(base, b)
    for b in blocks:
        sec = sec_by_id.get(b.section_id)
        _path = path_of.get(b.section_id, "") or (sec.title if sec else "")
        b.meta["chapter"] = _path.split(" > ")[0] if _path else ""
        b.meta["section"] = _path.split(" > ")[-1] if _path else ""
        base = re.sub(r"（续\d+）$", "", b.title or "")
        if base != (b.title or "") and base in by_base:
            b.meta["inherited_kind"] = by_base[base].kind or ""
        # 参考表（不是物品表）不靠词频投票：它就是"这一章的查表工具"，
        # 直接继承章节类别（战斗章的伤害表 → 规则，创建调查员章的表 → 玩家属性）
        if b.meta.get("is_table") and not b.meta.get("item_table") and "force_kind" not in b.meta:
            prior, _word = clf.title_prior(b.meta.get("section", ""), b.meta.get("chapter", ""))
            if prior:
                b.meta["force_kind"] = prior
        v = clf.classify(b, chapter_title=chapter_of.get(b.section_id, ""),
                         section_title=sec.title if sec else "")
        b.kind, b.subtype, b.confidence = v.kind, v.subtype, v.confidence
        b.meta["verdict"] = {"scores": v.scores, "evidence": v.evidence}
        if b.kind == "lore":
            b.meta["summary"] = extract.summarize(b.text, 2)
    timings["S3_blocks_classify"] = round(time.time() - t, 2)

    # ---------------- 落盘
    # 重新编译必须是"干净重来"：不删旧库的话，上一版里抽错的实体会原地留着
    # （entity 用名字哈希做主键，新结果不会覆盖没有对应名字的旧脏数据）。
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(out / "kb.sqlite") + suffix)
        if p.exists():
            p.unlink()
    con = db.connect(out / "kb.sqlite")
    schema = SchemaSkill(con)
    t = time.time()
    _write_book(con, book, prof, book_id, pdf_path)
    _write_sections(con, sections, book_id, path_of)
    src = _write_source(con, book, book_id, page_verdict)
    _log_non_game(con, book_id, denied_sections, denied_blocks, page_verdict,
                  (src.get("removed") or []) + scrubbed_lines)
    _write_blocks(con, blocks, book_id, src["page_text"])
    timings["S4_write"] = round(time.time() - t, 2)

    # ---------------- S5 抽取
    t = time.time()
    extract_stats = _run_extractors(con, book, sections, blocks, schema, book_id, chapter_of)
    timings["S5_extract"] = round(time.time() - t, 2)

    # ---------------- S6 图 + 场景
    t = time.time()
    kind_fixed = schema.refine_kinds()  # 拿真实值的分布校正字段类型（表头先到、数据后到）
    flows = _write_flows(con, blocks, schema)
    timeline = Timeline(con)
    campaign = _seed_campaign(con, timeline, book_id, blocks)
    timings["S6_graph"] = round(time.time() - t, 2)

    # ---------------- S7 索引
    t = time.time()
    vec_info = _write_vectors(con, blocks, with_vec)
    timings["S7_index"] = round(time.time() - t, 2)

    # ---------------- S8 自检
    t = time.time()
    gate_info = {
        "denied_sections": len(denied_sections),
        "denied_blocks": len(denied_blocks),
        "denied_pages": sum(1 for v in page_verdict.values() if not v.keep),
        "source": src,
        # 事后断言：知识层里不该再有版权/网址特征；非空就是闸门漏了
        "violations": gate.audit(con),
        "log": [
            dict(r)
            for r in con.execute(
                "SELECT scope,page,title,code,detail FROM non_game_log ORDER BY scope,page LIMIT 40"
            ).fetchall()
        ],
        "by_code": {
            r["code"]: r["c"]
            for r in con.execute("SELECT code,COUNT(*) c FROM non_game_log GROUP BY code ORDER BY c DESC")
        },
    }
    report = _report(con, book, prof, sections, blocks, schema, extract_stats, flows, vec_info, campaign, gate_info)
    report["extract"]["kind_fixed"] = kind_fixed
    report["selfcheck"] = selfcheck.to_dict()
    report["timings"] = timings
    report["total_seconds"] = round(time.time() - t_all, 1)
    report["book_id"] = book_id
    report["compiled_at"] = datetime.now().isoformat(timespec="seconds")
    db.set_meta(con, "report", json.dumps(report, ensure_ascii=False))
    con.commit()

    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    con.close()
    return report


# ------------------------------------------------------------------ 落盘

def _write_book(con, book, prof, book_id, pdf_path) -> None:
    con.execute(
        """INSERT OR REPLACE INTO book(id,title,doc_kind,source,sha256,pages,lang,compiled_at,compiler_version)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (book_id, book.title, prof.doc_kind, str(pdf_path), _sha256(pdf_path), book.page_count,
         "zh", datetime.now().isoformat(timespec="seconds"), COMPILER_VERSION),
    )


def _write_sections(con, sections, book_id, path_of) -> None:
    con.executemany(
        """INSERT OR REPLACE INTO section(id,book_id,parent_id,level,title,page_start,page_end,ord,source,path)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        [
            (s.id, book_id, s.parent_id, s.level, s.title, s.page_start, s.page_end, s.order,
             s.source, path_of.get(s.id, s.title))
            for s in sections
        ],
    )


def _seg_text(text: str) -> str:
    from .retrieve import tokenize

    return " ".join(tokenize(text))


def _write_source(con, book, book_id, page_verdict) -> dict:
    """原文层：逐页原样文本。**只写通过闸门的页**（非游戏内容一个字都不留）。

    返回 `page_text`（页码 → 原文），后面的块靠它在原文里定位字符区间。
    """
    page_text: dict[int, str] = {}
    rows = []
    removed: list[tuple[int, str, str]] = []
    for p in book.pages:
        v = page_verdict.get(p.no)
        if v is not None and not v.keep:
            continue
        txt = p.reading_text()
        # 原文层也擦掉署名/版权行：要的是"游戏内容的原文"，不是"带版权声明的原文"
        txt, dropped = gate.scrub_text(txt)
        removed.extend((p.no, "", ln) for ln in dropped)
        if not txt.strip():
            continue
        page_text[p.no] = txt
        rows.append((book_id, p.no, len(txt), txt))
    con.executemany(
        "INSERT OR REPLACE INTO source_page(book_id,page,chars,text) VALUES(?,?,?,?)", rows
    )
    return {"page_text": page_text, "pages": len(rows), "chars": sum(r[2] for r in rows), "removed": removed}


def _log_non_game(con, book_id, denied_sections, denied_blocks, page_verdict, scrubbed=None) -> None:
    """被拒的东西只留"页码 + 理由 + 指纹"，不留正文（这是需求里的硬要求）。"""
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for sec, v in denied_sections:
        rows.append((book_id, "section", sec.page_start, sec.title, v.code, v.detail, 0, "", now))
    for pno, v in page_verdict.items():
        if v.keep:
            continue
        rows.append((book_id, "page", pno, "", v.code, v.detail, 0, "", now))
    for b, v in denied_blocks:
        rows.append((book_id, "block", b.page, b.title, v.code, v.detail,
                     len(b.text or ""), gate.fingerprint(b.text), now))
    for page, title, line in (scrubbed or []):
        rows.append((book_id, "line", page, title, "credits_line", line[:80],
                     len(line), gate.fingerprint(line), now))
    con.executemany(
        """INSERT INTO non_game_log(book_id,scope,page,title,code,detail,chars,fingerprint,at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _write_blocks(con, blocks, book_id, page_text: dict[int, str] | None = None) -> None:
    rows, fts = [], []
    # 前言/目录/译名表里的年份是版权年、翻译年，不是故事里的世界时间，不能拿来当时间窗
    front = {
        r["id"]
        for r in con.execute(
            "SELECT id,title FROM section WHERE book_id=? AND "
            "(title LIKE '前言%' OR title LIKE '目录%' OR title LIKE '使用说明%' OR title LIKE '译名表%')",
            (book_id,),
        ).fetchall()
    }
    page_text = page_text or {}
    for b in blocks:
        y = None if b.section_id in front else extract_year(b.text)
        # 原文回指：块的首行/末行在"该页原文"里的字符区间，能直接 substr 回原文
        src_page, src_start, src_end = b.page, None, None
        txt = page_text.get(b.page) or ""
        if txt and b.lines:
            head, tail = b.lines[0].text, b.lines[-1].text
            i = txt.find(head)
            if i >= 0:
                j = txt.rfind(tail, i)
                src_start = i
                src_end = (j + len(tail)) if j >= 0 else (i + len(head))
        rows.append((b.id, book_id, b.section_id, b.order, b.page, b.page_end, b.kind, b.subtype,
                     b.title, b.text, b.meta.get("summary", ""), len(b.text), y, y,
                     b.confidence, json.dumps(b.meta, ensure_ascii=False),
                     src_page, src_start, src_end,
                     # canonical 与 kind 正交：0 = 示例/演示/作者注。
                     # 单列出来是为了让检索能直接 `WHERE canonical=1` 降权/过滤，
                     # 而不用把 meta JSON 拆开解析。
                     0 if (b.meta or {}).get("canonical") is False else 1))
        fts.append((b.id, b.title, _seg_text(b.text), b.meta.get("summary", "")))
    con.executemany(
        """INSERT OR REPLACE INTO block(id,book_id,section_id,ord,page,page_end,kind,subtype,title,text,
           summary,char_len,valid_from,valid_to,confidence,meta,src_page,src_start,src_end,canonical)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    con.executemany("INSERT INTO block_fts(block_id,title,text_seg,summary) VALUES(?,?,?,?)", fts)


# ------------------------------------------------------------------ 抽取编排

def _run_extractors(con, book, sections, blocks, schema: SchemaSkill, book_id: str, chapter_of) -> dict:
    stats = {}
    by_sec: dict[str, list] = {}
    for b in blocks:
        by_sec.setdefault(b.section_id, []).append(b)

    skill_ents, attr_ents, npc_ents, spell_ents, item_ents, rule_ents, lore_ents = ([] for _ in range(7))

    for sec in sections:
        sec_blocks = by_sec.get(sec.id, [])
        chapter = chapter_of.get(sec.id, "")
        title = sec.title
        stream = None

        if "技能列表" in title:
            stream = extract.build_stream(book, sec)
            skill_ents += extract.extract_skills(stream, sec, schema, con, book_id, sec_blocks)
        elif "创建调查员" in chapter and re.search(r"属性|第一步", title):
            stream = extract.build_stream(book, sec)
            attr_ents += extract.extract_attributes(stream, sec, schema, con, book_id, sec_blocks)
        elif "怪物" in chapter and re.search(r"神话生物|邪神|妖魔|野兽|怪物", title):
            # 属性表已经并进条目块时，用块抽（一块一份档案，归属不会串）；
            # 只有在这本书的属性表没被识别出来时，才退回按原文流抽。
            if not any((b.meta or {}).get("statblock") for b in sec_blocks):
                stream = extract.build_stream(book, sec)
                npc_ents += extract.extract_npcs(stream, sec, schema, con, book_id, sec_blocks)
        elif "法术" in chapter and "列表" in title:
            stream = extract.build_stream(book, sec)
            spell_ents += extract.extract_spells(stream, sec, schema, con, book_id, sec_blocks)

    # 清单型物品（"名称，价格"一行一条，物价表就是这种）
    item_ents += extract.extract_price_items(blocks, schema, con, book_id)

    # 怪物/人：一块一份档案（属性表已在切块阶段并回条目块）
    npc_ents += extract.extract_npcs_from_blocks(blocks, schema, con, book_id)

    # 物品：所有带表格的块（武器表、载具表、典籍表统一处理 —— 列名即字段名）
    for b in blocks:
        item_row = bool(b.meta and b.meta.get("item_table"))
        if (b.tables or item_row) and b.kind in ("tool", "npc", "rule", "lore"):
            item_ents += extract.extract_items_from_table(b, schema, con, book_id)

    # 规则实体 + 世界观
    for b in blocks:
        if b.kind == "rule":
            name = re.sub(r"^\d+(\.\d+)*\s*", "", b.title or "").strip() or "未命名规则"
            e = extract.upsert_entity(con, book_id, "rule", name, "rule", b.page,
                                      block_id=b.id, summary=(b.text or "")[:120], confidence=b.confidence,
                                      subtype=b.subtype)
            fd_body = schema.observe("rule", "原文", (b.text or "")[:40], origin="inline", page=b.page, quote=name)
            extract.add_fact(con, e, fd_body, (b.text or "")[:1500], name, b.page, b.id, conf=0.9)
            fd_sub = schema.observe("rule", "子类", b.subtype, origin="inline", page=b.page, quote=name)
            extract.add_fact(con, e, fd_sub, b.subtype, name, b.page, b.id)
            rule_ents.append(e)

    lore_blocks = [b for b in blocks if b.kind == "lore"]
    lore_ents = extract.extract_lore_blocks(lore_blocks, schema, con, book_id, chapter_of)

    stats.update(
        skill=len(skill_ents), attribute=len(attr_ents), npc=len(npc_ents),
        spell=len(spell_ents), item=len(item_ents), rule=len(rule_ents), lore=len(lore_ents),
    )
    stats["dropped_pseudo"] = _drop_pseudo_entities(con, book_id)
    stats["new_fields"] = schema.new_fields
    stats["merged_aliases"] = schema.merged_aliases
    return stats


def _write_flows(con, blocks, schema: SchemaSkill) -> dict:
    """规则 → 流程图；同时把"见第X页"这类交叉引用连成边。"""
    made = 0
    for b in blocks:
        if b.kind != "rule":
            continue
        name = re.sub(r"^\d+(\.\d+)*\s*", "", b.title or "").strip()
        if not name:
            continue
        f = extract.extract_rule_flow(b, schema, con)
        if not f:
            continue
        rule_id = extract.eid("rule", name)
        con.execute(
            "INSERT OR REPLACE INTO flow(rule_id,block_id,kind,nodes,edges,confidence) VALUES(?,?,?,?,?,?)",
            (rule_id, b.id, f["kind"], json.dumps(f["nodes"], ensure_ascii=False),
             json.dumps(f["edges"], ensure_ascii=False), 0.55),
        )
        made += 1

    # 交叉引用：正文里的"（RB263）/ 见第 143 页" → 指向那一页所属的章
    linked = 0
    for row in con.execute("SELECT id, text, section_id FROM block").fetchall():
        for m in re.finditer(r"(RB\d{2,4}|第\s*(\d{2,4})\s*页)", row["text"] or ""):
            num = m.group(2) or re.sub(r"\D", "", m.group(1))
            if not num:
                continue
            tgt = con.execute(
                "SELECT id FROM section WHERE ? BETWEEN page_start AND page_end LIMIT 1", (int(num),)
            ).fetchone()
            if tgt and tgt["id"] != row["section_id"]:
                con.execute(
                    "INSERT INTO edge(src,dst,rel,block_id,meta) VALUES(?,?,?,?,?)",
                    (row["section_id"], tgt["id"], "references", row["id"],
                     json.dumps({"raw": m.group(0)}, ensure_ascii=False)),
                )
                linked += 1
    return {"flows": made, "references": linked}


def _seed_campaign(con, timeline: Timeline, book_id: str, blocks: list) -> dict:
    """建一个默认团，场景从"模组"章里取。规则书也能跑 —— 场景少而已。"""
    cid = timeline.ensure_campaign(book_id, "default")
    scenes = timeline.scenes(book_id)
    return {"id": cid, "scenes": len(scenes), "first": scenes[0]["title"] if scenes else None}


def _write_vectors(con, blocks, enabled: bool) -> dict:
    if not enabled:
        return {"enabled": False, "reason": "关闭"}
    from .embedder import Embedder

    emb = Embedder()
    if not emb.available:
        return {"enabled": False, "reason": emb.reason}
    import numpy as np

    ok = 0
    batch = 32
    for i in range(0, len(blocks), batch):
        chunk = blocks[i : i + batch]
        texts = [f"{b.title}。{(b.text or '')[:400]}" for b in chunk]
        vecs = emb.encode(texts)
        if vecs is None:
            break
        for b, v in zip(chunk, vecs):
            con.execute(
                "INSERT OR REPLACE INTO block_vec(block_id,dim,vec) VALUES(?,?,?)",
                (b.id, emb.dim, np.asarray(v, dtype=np.float32).tobytes()),
            )
            ok += 1
    return {"enabled": True, "dim": emb.dim, "vectors": ok}


# ------------------------------------------------------------------ 自检报告

_SUSPECT_END = re.compile(r"[，、；,;]$")  # 冒号是列表引导语


def _spec_report(con) -> dict:
    """规范执行情况：五类的边界/证据/产物 + 当前归类是否合规 + 未分类清单。"""
    import json as _json

    sec = {r["id"]: r["path"] for r in con.execute("SELECT id,path FROM section").fetchall()}
    bad, unclassified, total = [], [], 0
    for r in con.execute("SELECT id,kind,text,title,meta,section_id,page FROM block").fetchall():
        total += 1
        meta = _json.loads(r["meta"] or "{}")
        path = sec.get(r["section_id"], "")
        meta["chapter"] = path.split(" > ")[0]
        meta["section"] = path.split(" > ")[-1]
        if r["kind"] == "unclassified":
            unclassified.append({"page": r["page"], "title": r["title"], "text": (r["text"] or "")[:70]})
            continue
        # 复核口径必须与判别口径一致，否则会出现"LLM 判了、校验过了、报告说违规"的自相矛盾。
        # 规则判别的块：要求命中该类证据特征（那是防止词频打分乱归的护栏）；
        # 模型判别的块：只查结构矛盾项（与 judge.check_classify 同一函数 —— 词表级
        # 典型性检查会制造"模型判了、护栏放了、报告又毙了"的死循环，实测 96.69% 那次）。
        by = (meta.get("verdict") or {}).get("by")
        if by == "llm":
            bad_reason = spec_mod.structural_forbidden(r["kind"] or "", r["text"] or "", meta)
            if r["kind"] not in spec_mod.SPEC:
                bad_reason = "非法类别"
            ok, probs = (not bad_reason), ([bad_reason] if bad_reason else [])
        else:
            ok, probs = spec_mod.validate(r["kind"] or "", r["text"] or "", meta)
        if not ok:
            bad.append({"page": r["page"], "title": r["title"], "kind": r["kind"],
                        "by": by or "rule", "why": probs})
    return {
        "kinds": [
            {"key": k, "zh": spec_mod.SPEC[k].zh, "boundary": spec_mod.SPEC[k].boundary,
             "evidence": spec_mod.SPEC[k].evidence_note, "forbid": spec_mod.SPEC[k].forbid_note,
             "payload": spec_mod.SPEC[k].payload}
            for k in spec_mod.KINDS
        ],
        "checked": total,
        "violations": bad[:20],
        "violation_count": len(bad),
        "unclassified": unclassified[:20],
        "unclassified_count": len(unclassified),
        "compliance": round(1 - len(bad) / max(1, total), 4),
        "text": spec_mod.spec_text(),
    }


def _report(con, book, prof, sections, blocks, schema, estats, flows, vec_info, campaign,
            gate_info: dict | None = None) -> dict:
    st = db.stats(con)
    pages_with_blocks = {b.page for b in blocks}
    uncovered = [p for p in range(1, book.page_count + 1) if p not in pages_with_blocks]
    suspect = [b for b in blocks if _SUSPECT_END.search((b.text or "").strip())]
    low_conf = [b for b in blocks if b.confidence < 0.45]

    # 各类抽一个样本，报告里能直接看"编译出来是什么样"
    samples = {}
    for etype in ("skill", "item", "monster", "spell", "rule", "lore", "attribute"):
        row = con.execute(
            "SELECT id,name,page FROM entity WHERE etype=? ORDER BY confidence DESC, page LIMIT 1",
            (etype,),
        ).fetchone()
        if row:
            samples[etype] = schema.card(row["id"])
            if samples[etype].get("source"):
                samples[etype]["source"]["text"] = samples[etype]["source"]["text"][:600]

    flow_row = con.execute("SELECT * FROM flow ORDER BY confidence DESC LIMIT 1").fetchone()
    flow_sample = None
    if flow_row:
        flow_sample = {
            "rule_id": flow_row["rule_id"],
            "kind": flow_row["kind"],
            "nodes": json.loads(flow_row["nodes"])[:8],
            "edges": json.loads(flow_row["edges"])[:10],
        }

    return {
        "title": prof.book_title,
        "pages": book.page_count,
        "layout": prof.to_json(),
        "layout_human": layout.describe(prof),
        "stats": st,
        "extract": estats,
        "graph": flows,
        "vectors": vec_info,
        "campaign": campaign,
        "quality": {
            "pages_without_block": uncovered[:40],
            "pages_without_block_count": len(uncovered),
            "suspect_blocks": len(suspect),
            "suspect_examples": [
                {"block": b.id, "page": b.page, "title": b.title, "tail": (b.text or "")[-40:]}
                for b in suspect[:5]
            ],
            "low_confidence_blocks": len(low_conf),
            "low_conf_examples": [
                {"page": b.page, "title": b.title, "kind": b.kind, "conf": b.confidence} for b in low_conf[:5]
            ],
        },
        "spec": _spec_report(con),
        "samples": samples,
        "flow_sample": flow_sample,
        "gate": gate_info or {},
        "schema_audit": schema.audit(15),
        "field_dict": [
            {"key": f.key, "label": f.label, "kind": f.kind, "unit": f.unit,
             "origin": f.origin, "first_page": f.first_page, "aliases": f.aliases[:4],
             "quote": (f.evidence_quote or "")[:80]}
            for f in sorted(schema._cache.values(), key=lambda x: (x.scope, x.first_page or 0))[:120]
        ],
    }


_PSEUDO_NAMES = {"STR", "CON", "SIZ", "DEX", "INT", "POW", "APP", "EDU", "LUK", "LUCK"}


def _drop_pseudo_entities(con, book_id: str) -> int:
    """删掉"属性缩写被当成实体"的垃圾（INT/POW 这类）。

    它们只可能来自表格行/表头被当成实体名（表头是字段，不是实体）。判定条件极窄：
    名字正好是属性缩写、且类型是 item/rule/npc/monster/spell（attribute 类型保留 ——
    那是"力量（STR）"这类真正在讲属性的条目）。
    """
    rows = con.execute(
        "SELECT id,name FROM entity WHERE book_id=? AND UPPER(name) IN (%s) "
        "AND etype IN ('item','rule','npc','monster','spell')"
        % ",".join("?" * len(_PSEUDO_NAMES)),
        (book_id, *_PSEUDO_NAMES),
    ).fetchall()
    for r in rows:
        con.execute("DELETE FROM fact WHERE entity_id=?", (r["id"],))
        con.execute("DELETE FROM entity WHERE id=?", (r["id"],))
    return len(rows)
