"""命令行入口。

```
python -m compiler.cli profile --pdf 某本书.pdf          # 只摸底：只看版式画像，不编译
python -m compiler.cli ingest  --pdf 某本书.pdf --out data/kb/coc7e
python -m compiler.cli search  --kb data/kb/coc7e "理智检定失败会怎样" --year 1925
python -m compiler.cli card    --kb data/kb/coc7e 拜亚基
python -m compiler.cli flow    --kb data/kb/coc7e 战斗轮
python -m compiler.cli demo    --kb data/kb/coc7e      # 按 DM 的典型提问跑一圈
```

`profile` 的存在是有意的：接一本**没见过的新类型文档**时，先跑这一条看画像打分，
确认标题家族选得对不对，再决定要不要加一个 HeadingFamily。不用改编译流程。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import layout as layout_mod
from . import reader


def cmd_profile(args) -> int:
    book = reader.load(args.pdf, max_pages=args.limit)
    prof = layout_mod.profile(book, save_to=args.out)
    print(layout_mod.describe(prof))
    if args.json:
        print(json.dumps(prof.to_json(), ensure_ascii=False, indent=2))
    return 0


def cmd_ingest(args) -> int:
    from .pipeline import compile_book

    rep = compile_book(args.pdf, args.out, book_id=args.book_id, limit_pages=args.limit,
                       with_vec=not args.no_vec)
    print(rep["layout_human"])
    st = rep["stats"]
    print(
        f"\n编译完成：{rep['pages']} 页 / {st['section']} 章节 / {st['block']} 块 / "
        f"{st['entity']} 实体 / {st['fact']} 事实 / {st['field']} 字段 / {rep['total_seconds']}s"
    )
    print("分类分布：", st["by_kind"])
    print("实体分布：", st["by_etype"])
    print("字段作用域：", st["by_scope"])
    print("抽取：", rep["extract"])
    print("向量：", rep["vectors"])
    g = rep.get("gate") or {}
    if g:
        print(
            f"闸门：拒 {g.get('denied_sections')} 节 / {g.get('denied_pages')} 页 / "
            f"{g.get('denied_blocks')} 块；原因 {g.get('by_code')}；"
            f"知识层违规 {len(g.get('violations') or [])} 处"
        )
        src = g.get("source") or {}
        print(f"原文层：{src.get('pages')} 页 / {src.get('chars')} 字（只存游戏内容）")
    sc = rep.get("selfcheck") or {}
    if sc:
        b4, af = sc.get("before") or {}, sc.get("after") or {}
        print(
            f"自检：{sc.get('rounds')} 轮 · {'通过' if sc.get('passed') else '未通过'} · "
            f"残留 {sc.get('residual_rate', 0):.2%} · "
            f"超长 {b4.get('over_long')}→{af.get('over_long')} · "
            f"碎片 {b4.get('fragments')}→{af.get('fragments')} · "
            f"多条 {b4.get('multi_entry')}→{af.get('multi_entry')}"
        )
    q = rep["quality"]
    print(f"覆盖：{q['pages_without_block_count']} 页没有块；可疑块 {q['suspect_blocks']}；低置信块 {q['low_confidence_blocks']}")
    for w in rep["layout"].get("warnings", []):
        print("⚠", w)
    print(f"\n产物：{Path(args.out).resolve()}")
    return 0


def _open_kb(args):
    from .retrieve import KB

    path = Path(args.kb) / "kb.sqlite"
    if not path.exists():
        print(f"没有找到知识库：{path}（先跑 ingest）", file=sys.stderr)
        raise SystemExit(2)
    return KB(str(path), with_vec=not args.no_vec)


def cmd_search(args) -> int:
    kb = _open_kb(args)
    kinds = [args.kind] if args.kind else None
    hits = kb.search(args.query, k=args.k, kinds=kinds, year=args.year)
    if not hits:
        print("没有命中")
        return 0
    for h in hits:
        print(f"\n[{h.score:.3f}] p{h.page} · {h.section} · {h.kind}/{h.subtype} · {h.why}")
        print("  " + h.text.replace("\n", " ")[:args.chars])
    return 0


def cmd_card(args) -> int:
    kb = _open_kb(args)
    print(json.dumps(kb.card(args.name, etype=args.type), ensure_ascii=False, indent=2))
    return 0


def cmd_flow(args) -> int:
    kb = _open_kb(args)
    f = kb.flow(args.name)
    print(json.dumps(f, ensure_ascii=False, indent=2) if f else "没有为这条规则抽出流程图")
    return 0


DEMO_QUERIES = [
    ("工具类·物品名索引", "item", "加利尔突击步枪"),
    ("玩家属性·技能直查", "skill", "侦查"),
    ("NPC·怪物档案（只读）", "npc", "深潜者"),
    ("规则·判定步骤+流程图", "rule", "对抗检定"),
    ("世界观·摘要+原文聚类", "lore", "克苏鲁"),
    ("跨类语义检索·换说法", "search", "被怪物吓到精神崩溃会怎么样"),
]


def cmd_demo(args) -> int:
    """五类容器各查一条 —— 这是"DM 能不能直接拿到成品"的自证。"""
    kb = _open_kb(args)
    for label, kind, arg in DEMO_QUERIES:
        print(f"\n=== {label}：kb.{kind}({arg!r}) ===")
        if kind == "item":
            card = kb.item_card(arg)
            if "error" in card:
                print("  ", card["error"])
                continue
            print(f"  {card['name']}（p{card.get('page')}）字段 {len(card.get('fields') or [])} 个")
            for f in (card.get("fields") or [])[:6]:
                print(f"    · {f['key']} = {str(f['value'])[:40]}")
        elif kind == "skill":
            card = kb.skill_card(arg)
            if "error" in card:
                print("  ", card["error"])
                continue
            print(f"  {card['name']} 基础值 {card.get('base_value')} p{card.get('page')}")
            print(f"    {str(card.get('description') or '')[:100]}")
        elif kind == "npc":
            card = kb.npc_card(arg)
            if "error" in card:
                print("  ", card["error"])
                continue
            print(f"  {card['name']}（{card['etype']}，p{card.get('page')}）属性 {len(card.get('fields') or [])} 条")
            for f in (card.get("fields") or [])[:6]:
                print(f"    · {f['key']} = {f['value']} {f.get('dice') or ''}")
            for a in (card.get("abilities") or [])[:4]:
                print(f"    · 能力 {a['name']}：{str(a['text'])[:50]}")
        elif kind == "rule":
            card = kb.rule_card(arg)
            if "error" in card:
                print("  ", card["error"])
                continue
            print(f"  {card['name']}｜子类 {card.get('subtype')}｜步骤 {len(card.get('steps') or [])}")
            for s in (card.get("steps") or [])[:5]:
                print(f"    {s['seq'] + 1}. [{s['kind']}] {s['text']}")
            if card.get("flow"):
                print(f"    流程图：{len(card['flow']['nodes'])} 节点 / {len(card['flow']['edges'])} 边")
        elif kind == "lore":
            topics = kb.lore_topics(arg, k=3)
            for t in topics[:3]:
                if "fallback" in t:
                    print("  ", t["fallback"][:100])
                    continue
                print(f"  主题「{t['topic']}」（{t['members']} 段原文）：{t['summary']}")
                for o in (t.get("originals") or [])[:2]:
                    print(f"    p{o['page']} sim={o['sim']}: {o['text'][:60]}")
        else:
            hits = kb.search(arg, k=3)
            for h in hits:
                print(f"  p{h.page} [{h.score:.2f}] {h.title}：{h.text[:70]}")
            if not hits:
                print("  (无命中)")
    return 0


def cmd_report(args) -> int:
    """把 report.json 渲染成一页 HTML（自包含，可直接打开）。"""
    from .report_html import render

    kb = Path(args.kb)
    rep_path = kb / "report.json"
    if not rep_path.exists():
        print(f"没有找到 {rep_path}（先跑 ingest）", file=sys.stderr)
        return 2
    import json as _json

    report = _json.loads(rep_path.read_text(encoding="utf-8"))
    out = render(report, args.out or (kb / "report.html"))
    print(f"已生成 {out}")
    return 0


TABLE_NOTES = {
    "meta": "键值杂项（etypes 列表、编译报告摘要）",
    "book": "书本身：标题/类型/页数/哈希/版本",
    "section": "★ 粗分：章节树（page_start~page_end + path 出处）",
    "block": "★ 细分：检索单元，text 是原文，kind/subtype 是五大类",
    "entity": "★ 实体：物品/技能/NPC/规则/世界观（readonly=1 = 书里定义）",
    "field": "★★ 字段字典：Agent 读出来的表结构，带 evidence_quote 出生证明",
    "fact": "★ 事实：(实体, 字段, 值) + 页码 + 原文引用",
    "edge": "关系：章节间交叉引用（见第X页）",
    "flow": "规则流程图：nodes/edges 是 JSON",
    "campaign": "团：当前场景 / 世界时间 / 游标",
    "timeline_event": "剧情时间线：只追加的事件流",
    "entity_state": "实体在本团的状态（真相源，死者留墓碑）",
    "schema_log": "字段拓展审计流：new / alias / kind_fix / new_etype",
    "block_fts": "BM25 全文索引（jieba 预切词）",
    "block_fts_data": "FTS5 内部数据",
    "block_fts_idx": "FTS5 内部数据",
    "block_fts_content": "FTS5 内部数据",
    "block_fts_docsize": "FTS5 内部数据",
    "block_fts_config": "FTS5 内部数据",
    "block_vec": "向量：每块 512 维 float32",
}


def _ro_conn(path: Path):
    """只读打开：**查看器不该有能力改坏知识库**。"""
    import sqlite3

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def cmd_tables(args) -> int:
    """列出所有表 + 行数 + 字段名（数据集里"哪张表装什么"的一张地图）。"""
    kb = Path(args.kb)
    db = kb / "kb.sqlite"
    if not db.exists():
        print(f"没有找到 {db}（先跑 ingest）", file=sys.stderr)
        return 2
    con = _ro_conn(db)
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    print(f"{db.resolve()}\n")
    print(f"{'表':<18}{'行数':>7}  {'说明 / 字段'}")
    print("-" * 100)
    for r in rows:
        name = r["name"]
        try:
            n = con.execute(f'SELECT COUNT(*) c FROM "{name}"').fetchone()["c"]
        except Exception:  # noqa: BLE001
            n = -1
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{name}")').fetchall()]
        note = TABLE_NOTES.get(name, "")
        print(f"{name:<18}{n:>7}  {note}")
        if cols and name in TABLE_NOTES and not name.startswith("block_fts"):
            print(f"{'':<18}{'':>7}  └ {', '.join(cols)}")
    con.close()
    return 0


def _fmt(v, width: int = 60) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= width else s[: width - 1] + "…"


def cmd_show(args) -> int:
    """看某张表里到底有什么：默认挑最有信息量的列，避免一屏全是 id。"""
    kb = Path(args.kb)
    db = kb / "kb.sqlite"
    if not db.exists():
        print(f"没有找到 {db}", file=sys.stderr)
        return 2
    con = _ro_conn(db)
    name = args.table
    where = f" WHERE {args.where}" if args.where else ""
    order = f" ORDER BY {args.order}" if args.order else ""
    try:
        rows = con.execute(f'SELECT * FROM "{name}"{where}{order} LIMIT {args.limit}').fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"查询失败：{exc}", file=sys.stderr)
        return 2
    if not rows:
        print("（没有数据）")
        return 0
    cols = list(rows[0].keys())
    if args.cols:
        cols = [c.strip() for c in args.cols.split(",") if c.strip() in cols] or cols
    widths = {c: min(60, max(len(c), *(len(_fmt(r[c], 60)) for r in rows))) for c in cols}
    print(" | ".join(f"{c:<{widths[c]}}" for c in cols))
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(f"{_fmt(r[c], 60):<{widths[c]}}" for c in cols))
    total = con.execute(f'SELECT COUNT(*) c FROM "{name}"{where}').fetchone()["c"]
    print(f"\n（显示 {len(rows)}/{total} 行）")
    con.close()
    return 0


def cmd_sql(args) -> int:
    """任意只读 SQL。只放行 SELECT / WITH / PRAGMA —— 查看器不能变成编辑器。"""
    kb = Path(args.kb)
    db = kb / "kb.sqlite"
    if not db.exists():
        print(f"没有找到 {db}", file=sys.stderr)
        return 2
    q = args.query.strip()
    if not q.lower().startswith(("select", "with", "pragma", "explain")):
        print("只允许 SELECT / WITH / PRAGMA / EXPLAIN（只读）", file=sys.stderr)
        return 2
    con = _ro_conn(db)
    try:
        rows = con.execute(q).fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"SQL 失败：{exc}", file=sys.stderr)
        return 2
    if not rows:
        print("（没有结果）")
        return 0
    cols = list(rows[0].keys())
    print(" | ".join(cols))
    print("-+-".join("-" * max(3, len(c)) for c in cols))
    for r in rows[: args.limit]:
        print(" | ".join(_fmt(r[c], 70) for c in cols))
    print(f"\n（{len(rows)} 行）")
    con.close()
    return 0


def cmd_lore(args) -> int:
    """常识与世界观：按子类分组的 topic 列表（摘要只是钩子，原文在 fact 里）。"""
    import sqlite3

    db = Path(args.kb) / "kb.sqlite"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = "SELECT subtype,name,page,summary,(SELECT COUNT(*) FROM fact f WHERE f.entity_id=e.id) n FROM entity e WHERE etype='lore'"
    params: list = []
    if args.subtype:
        sql += " AND subtype=?"
        params.append(args.subtype)
    if args.grep:
        sql += " AND (name LIKE ? OR summary LIKE ?)"
        params += [f"%{args.grep}%", f"%{args.grep}%"]
    rows = con.execute(sql + " ORDER BY subtype, page LIMIT ?", params + [args.limit]).fetchall()
    cur = None
    for r in rows:
        if r["subtype"] != cur:
            cur = r["subtype"]
            print(f"\n【{cur or '未分类'}】")
        print(f"  p{r['page']:<4} {r['name']:<18} 事实{r['n']}  {(r['summary'] or '')[:46]}")
    total = con.execute("SELECT COUNT(*) c FROM entity WHERE etype='lore'").fetchone()["c"]
    by = con.execute(
        "SELECT subtype,COUNT(*) c FROM entity WHERE etype='lore' GROUP BY subtype ORDER BY c DESC"
    ).fetchall()
    print(f"\n合计 {total} 条：" + "，".join(f"{r['subtype'] or '未分类'} {r['c']}" for r in by))
    con.close()
    return 0


def cmd_story(args) -> int:
    """剧情/世界线：书里能当"场景"的节点（来自模组章，不预设剧本格式）。"""
    from .retrieve import KB
    from .timeline import Timeline

    kb = KB(str(Path(args.kb) / "kb.sqlite"), with_vec=False)
    bid = kb.con.execute("SELECT id FROM book").fetchone()["id"]
    scenes = Timeline(kb.con).scenes(bid)
    for s in scenes[: args.limit]:
        print(f"  p{s['page']:<4} {s['title'][:60]}")
    print(f"\n共 {len(scenes)} 个场景节点（p{scenes[0]['page']}–{scenes[-1]['page']}）" if scenes else "没有剧情章")
    kb.close()
    return 0


def cmd_timeline(args) -> int:
    """世界线（可写层）：团游标 + 事件流 + 实体状态。"""
    from .retrieve import KB
    from .timeline import Timeline

    kb = KB(str(Path(args.kb) / "kb.sqlite"), with_vec=False)
    tl = Timeline(kb.con)
    bid = kb.con.execute("SELECT id FROM book").fetchone()["id"]
    camps = kb.con.execute("SELECT id,name,world_time FROM campaign").fetchall()
    if not camps:
        print("还没有团（用 --start 建一个）")
        return 0
    cid = args.campaign or camps[0]["id"]
    if args.start:
        started = tl.start(bid, args.start)
        cid = started["campaign"]  # 刚建的团才是要看的那一个
        print("开场：", (started.get("current") or {}).get("title"))
    if args.advance:
        p = tl.advance(cid, note=args.note or "")
        print("已推进到：", (p["current"] or {}).get("title"))
    prog = tl.progress(cid)
    cur = prog.get("current") or {}
    print(f"团 {cid}｜世界时间 {prog.get('world_time') or '未设置'}｜进度 {prog.get('scene_done')}/{prog.get('scene_total')}")
    print(f"当前场景：p{cur.get('page', '—')} {cur.get('title') or '（未开始）'}")
    print("接下来：" + "、".join(f"{s['title'][:24]}(p{s['page']})" for s in prog.get("next", [])))
    ev = tl.replay(cid, 12)
    if ev:
        print("\n事件流：")
        for e in ev:
            print(f"  #{e['seq']} [{e['kind']}] {e['title']} · {e['body'][:40]}")
    states = kb.con.execute(
        "SELECT es.status, e.name FROM entity_state es JOIN entity e ON e.id=es.entity_id WHERE es.campaign_id=?",
        (cid,),
    ).fetchall()
    if states:
        print("\n状态真相源：" + "，".join(f"{s['name']}={s['status']}" for s in states))
    kb.close()
    return 0


def cmd_raw(args) -> int:
    """看原文层：逐页原样文本（这就是"保存原始文本便于直接查看"那一层）。"""
    import sqlite3

    db = Path(args.kb) / "kb.sqlite"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    if args.page:
        r = con.execute("SELECT page,chars,text FROM source_page WHERE page=?", (args.page,)).fetchone()
        if not r:
            print(f"p{args.page} 不在原文层（多半被闸门判成非游戏内容，见 `cli gate`）")
            return 0
        print(f"—— 原文 p{r['page']}（{r['chars']} 字）——\n")
        print(r["text"])
        return 0
    if args.grep:
        rows = con.execute(
            "SELECT page, text FROM source_page WHERE text LIKE ? ORDER BY page LIMIT ?",
            (f"%{args.grep}%", args.limit),
        ).fetchall()
        for r in rows:
            t = r["text"]
            i = t.find(args.grep)
            print(f"p{r['page']}: …{t[max(0,i-40):i+60]}…".replace("\n", " "))
        print(f"\n命中 {len(rows)} 页")
        return 0
    n, c = con.execute("SELECT COUNT(*), SUM(chars) FROM source_page").fetchone()
    print(f"原文层：{n} 页 / {c} 字")
    print("用法：--page 260 看某页；--grep 关键词 搜原文")
    con.close()
    return 0


def cmd_export(args) -> int:
    """把原文层导成一份 Markdown：**人读/校对用**，不是给模型吃的。"""
    import sqlite3

    db = Path(args.kb) / "kb.sqlite"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT page,text FROM source_page ORDER BY page").fetchall()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# 原文导出（{len(rows)} 页，仅游戏内容）\n\n")
        for page, text in rows:
            f.write(f"\n\n<!-- p{page} -->\n\n")
            f.write(text)
    print(f"已导出 {out}（{len(rows)} 页）")
    con.close()
    return 0


def cmd_spec(args) -> int:
    """打印正文内容规范（硬约束），以及给 LLM 的分类提示词样例。"""
    from . import spec as spec_mod

    if args.prompt_for:
        import sqlite3

        db = Path(args.kb) / "kb.sqlite"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        r = con.execute(
            "SELECT b.text, s.path FROM block b LEFT JOIN section s ON s.id=b.section_id "
            "WHERE b.page=? OR b.title LIKE ? LIMIT 1",
            (args.prompt_for, f"%{args.prompt_for}%"),
        ).fetchone()
        print(spec_mod.build_prompt(r[0] if r else "", r[1] if r else ""))
        return 0
    print(spec_mod.spec_text())
    return 0


def cmd_graph(args) -> int:
    """用 LangGraph 跑一遍完整流程（标准图式编译，带进度与断点）。"""
    from .graph import run_compile

    def show(name, upd):
        for t in upd.get("trace") or []:
            bar = "·" * max(0, 12 - len(name))
            print(f"  {name:<12s}{bar} {t['ms']:>6}ms  {t['note']}")

    opts = {"max_chars": args.max_chars, "use_llm": bool(args.use_llm)}
    st = run_compile(
        args.pdf, args.out,
        book_id=args.book_id,
        limit_pages=args.limit,
        with_vec=not args.no_vec,
        options=opts,
        thread_id=args.thread,
        checkpointer="memory" if args.memory_checkpoint else "sqlite",
        on_node=show,
    )
    rep = st.get("report") or {}
    v = st.get("verdict") or {}
    stt = rep.get("stats") or {}
    print()
    print(f"阶段：{st.get('stage')}｜线程：{st.get('thread_id')}")
    if st.get("errors"):
        print("错误：", "；".join(st["errors"]))
        return 1
    print(f"产出：{stt.get('block')} 块 / {stt.get('entity')} 实体 / {stt.get('field')} 字段 / "
          f"{stt.get('fact')} 事实 / 原文 {stt.get('source_page')} 页")
    print(f"自证：{'通过' if v.get('passed') else '未通过'}｜{'；'.join(v.get('reasons') or [])}")
    print(f"指标：{v.get('metrics')}")
    print(f"库：{(st.get('store') or {}).get('kb_path')}")
    return 0 if v.get("passed") else 2


def cmd_resume(args) -> int:
    """从检查点续跑（上次被打断、或想接着跑完剩下的阶段）。"""
    from .graph import resume_compile

    def show(name, upd):
        for t in upd.get("trace") or []:
            print(f"  {name:<12s} {t['ms']:>6}ms  {t['note']}")

    st = resume_compile(args.out, args.thread, on_node=show)
    v = st.get("verdict") or {}
    print(f"阶段：{st.get('stage')}｜自证：{'通过' if v.get('passed') else (v.get('reasons') or '未完成')}")
    return 0 if v.get("passed") else 2


def cmd_containers(args) -> int:
    """阶段二：把五类容器物化出来（docs/知识库目标结构.md）。"""
    import os

    from . import containers

    kb = Path(args.kb)
    db_path = kb / "kb.sqlite" if kb.is_dir() else kb
    if not db_path.exists():
        print(f"没有这个库：{db_path}")
        return 1
    book_id = args.book_id
    if not book_id:
        import sqlite3

        con = sqlite3.connect(str(db_path))
        row = con.execute("SELECT id FROM book LIMIT 1").fetchone()
        con.close()
        book_id = row[0] if row else ""
    model = None
    if not args.no_llm:
        os.environ.setdefault("RPGBAR_LLM", "")
        from .graph.llm_classify import get_default_model

        model = get_default_model()
        if model is None:
            print("没有模型端点（RPGBAR_LLM），改为只做结构拼装")
    stats = containers.build_all(str(db_path), book_id, model=model, lore_threshold=args.lore_threshold)
    for k, v in stats.items():
        print(f"{k:>6s}  {v}")
    return 0


def cmd_mermaid(args) -> int:
    """打印图结构（Mermaid），贴文档用。"""
    from .graph import graph_mermaid

    print(graph_mermaid())
    return 0


def cmd_browse(args) -> int:
    """把切分结果渲染成可翻阅的网页（块 + 原文层）。"""
    from .browse import render

    kb = Path(args.kb)
    out = render(kb / "kb.sqlite", args.out or (kb / "browse.html"))
    print(f"已生成 {out}")
    return 0


def cmd_gate(args) -> int:
    """闸门报告：拒了什么、为什么、知识层有没有漏网。"""
    import sqlite3

    db = Path(args.kb) / "kb.sqlite"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print("按原因：", {r["code"]: r["c"] for r in con.execute(
        "SELECT code,COUNT(*) c FROM non_game_log GROUP BY code ORDER BY c DESC")})
    rows = con.execute(
        "SELECT scope,page,title,code,substr(detail,1,48) d FROM non_game_log ORDER BY scope,page"
    ).fetchall()
    for r in rows:
        print(f"  [{r['code']:<12}] {r['scope']:<7} p{r['page']:<4} {r['title'][:12]:<14} {r['d'] or ''}")
    src = con.execute("SELECT COUNT(*) n, SUM(chars) c FROM source_page").fetchone()
    print(f"\n原文层：{src['n']} 页 / {src['c']} 字")
    # 事后断言
    from . import gate as gate_mod

    bad = gate_mod.audit(con)
    print(f"知识层违规：{len(bad)} 处" + ("" if not bad else "  ← 闸门漏了！"))
    for b in bad[:10]:
        print("  ", b)
    con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="compiler", description="剧本/规则书编译 Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("profile", help="只摸版式：出画像，不编译")
    p.add_argument("--pdf", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("ingest", help="编译一本书（旧序贯管线，保留用于对照）")
    p.add_argument("--pdf", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--book-id", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-vec", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("graph", help="★ 用 LangGraph 编译（标准图流程，可断点续跑）")
    p.add_argument("--pdf", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--book-id", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-vec", action="store_true")
    p.add_argument("--max-chars", type=int, default=520)
    p.add_argument("--use-llm", action="store_true", help="启用 LLM 复核（需 RPGBAR_LLM）")
    p.add_argument("--thread", default=None, help="线程 id；同名可续跑")
    p.add_argument(
        "--memory-checkpoint", action="store_true",
        help="检查点只放内存（快、不落盘，但不能跨进程续跑；几百页以上的书建议默认落盘）",
    )
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("resume", help="从检查点续跑")
    p.add_argument("--out", required=True)
    p.add_argument("--thread", required=True)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("mermaid", help="打印图结构（Mermaid）")
    p.set_defaults(func=cmd_mermaid)

    for name, fn, extra in (
        ("search", cmd_search, [("query", None)]),
        ("card", cmd_card, [("name", None)]),
        ("flow", cmd_flow, [("name", None)]),
    ):
        p = sub.add_parser(name)
        for a, d in extra:
            p.add_argument(a, **({"default": d} if d else {}))
        p.add_argument("--kb", required=True)
        p.add_argument("--no-vec", action="store_true")
        if name == "search":
            p.add_argument("--k", type=int, default=6)
            p.add_argument("--kind", default=None, choices=["rule", "pc", "tool", "npc", "lore"])
            p.add_argument("--year", type=int, default=None)
            p.add_argument("--chars", type=int, default=260)
        if name == "card":
            p.add_argument("--type", default=None)
        p.set_defaults(func=fn)

    p = sub.add_parser("demo", help="按 DM 的典型提问跑一圈")
    p.add_argument("--kb", required=True)
    p.add_argument("--no-vec", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("report", help="把编译报告渲染成 HTML")
    p.add_argument("--kb", required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_report)

    # ---- 查看库（tables / show / sql）----
    p = sub.add_parser("tables", help="列出所有表、行数、字段（库的地图）")
    p.add_argument("--kb", required=True)
    p.set_defaults(func=cmd_tables)

    p = sub.add_parser("show", help="看某张表里的内容")
    p.add_argument("table", help="表名，如 section / block / entity / field / fact")
    p.add_argument("--kb", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--where", default=None, help="SQL 条件，如 \"kind='npc' AND page>250\"")
    p.add_argument("--order", default=None, help="排序，如 page")
    p.add_argument("--cols", default=None, help="只显示这些列，逗号分隔")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("sql", help="跑一条只读 SQL")
    p.add_argument("query")
    p.add_argument("--kb", required=True)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_sql)

    # ---- 常识与世界观 / 世界线 ----
    p = sub.add_parser("lore", help="常识与世界观：按子类列 topic")
    p.add_argument("--kb", required=True)
    p.add_argument("--subtype", default=None)
    p.add_argument("--grep", default=None, help="按名字或摘要模糊过滤")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_lore)

    p = sub.add_parser("story", help="剧情场景节点（世界线的静态骨架）")
    p.add_argument("--kb", required=True)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_story)

    p = sub.add_parser("timeline", help="世界线（团游标/事件流/实体状态）")
    p.add_argument("--kb", required=True)
    p.add_argument("--campaign", default=None)
    p.add_argument("--start", default=None, help="建一个团名并开场")
    p.add_argument("--advance", action="store_true", help="推进到下一个场景")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_timeline)

    # ---- 原文层 / 闸门 ----
    p = sub.add_parser("raw", help="看原文层（逐页原样文本）")
    p.add_argument("--kb", required=True)
    p.add_argument("--page", type=int, default=None)
    p.add_argument("--grep", default=None)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("export", help="把原文层导成 Markdown")
    p.add_argument("--kb", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("spec", help="打印正文内容规范（硬约束）/ 生成 LLM 分类提示词")
    p.add_argument("--kb", required=True)
    p.add_argument("--prompt-for", default=None, help="页码或标题关键字：输出该块的分类提示词")
    p.set_defaults(func=cmd_spec)

    p = sub.add_parser("browse", help="生成可翻阅的网页（切分结果 + 原文层）")
    p.add_argument("--kb", required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_browse)

    p = sub.add_parser("gate", help="看非游戏内容拦截清单与自查结果")
    p.add_argument("--kb", required=True)
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("containers", help="★ 阶段二：把五类容器物化出来（规则树/技能/物品/NPC/世界观）")
    p.add_argument("--kb", required=True, help="知识库目录（含 kb.sqlite）")
    p.add_argument("--book-id", default=None)
    p.add_argument("--no-llm", action="store_true", help="不连模型：只用已在库里的结构拼装")
    p.add_argument("--lore-threshold", type=float, default=0.66)
    p.set_defaults(func=cmd_containers)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
