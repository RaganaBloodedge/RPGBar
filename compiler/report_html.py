"""把 report.json 渲染成一页 HTML（自包含、浅色、可直接给人看）。

为什么要单独出一个 HTML：编译报告的价值全在"能一眼看出哪儿对了、哪儿错了"。
终端里滚 300 行不如一张图 —— 版式画像的打分、字段字典的出生证明、样例卡片、
流程图，这些天生是版式化的东西。
"""
from __future__ import annotations

import html
import json
from pathlib import Path

CSS = """
:root{
  --bg:#fbfaf8; --card:#fff; --ink:#1c1b1a; --ink2:#5c5852; --line:#e6e2db;
  --accent:#8a5a2b; --accent2:#2f6b5f; --warn:#a8501f; --soft:#f4f0e9;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"PingFang SC","Microsoft YaHei",-apple-system,Segoe UI,sans-serif;
  font-size:14px;line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:56px 32px 96px}
h1{font-size:30px;line-height:1.25;margin:0 0 6px;letter-spacing:-.4px;font-weight:600}
h2{font-size:18px;margin:56px 0 14px;font-weight:600;letter-spacing:-.2px;
  display:flex;align-items:center;gap:10px}
h2::before{content:"";width:3px;height:16px;background:var(--accent);border-radius:2px}
h3{font-size:14px;margin:22px 0 8px;color:var(--ink2);font-weight:600}
.sub{color:var(--ink2);margin:0 0 28px;font-size:13.5px}
.grid{display:grid;gap:14px}
.g4{grid-template-columns:repeat(4,1fr)}
.g2{grid-template-columns:1fr 1fr}
.g3{grid-template-columns:repeat(3,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.kpi .n{font-size:26px;font-weight:600;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.kpi .l{font-size:12px;color:var(--ink2);margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--ink2);font-size:12px;background:var(--soft)}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
code,.mono{font-family:"JetBrains Mono",Consolas,Menlo,monospace;font-size:12.5px}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
  background:var(--soft);color:var(--ink2);border:1px solid var(--line)}
.tag.ok{background:#eef5f2;color:var(--accent2);border-color:#d6e6e0}
.tag.warn{background:#fbf0e8;color:var(--warn);border-color:#eedccd}
.bar{height:6px;background:var(--soft);border-radius:3px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:var(--accent);border-radius:3px}
.bar.alt>i{background:var(--accent2)}
.flow{display:flex;align-items:stretch;gap:0;flex-wrap:wrap;margin:18px 0 4px}
.step{flex:1 1 0;min-width:96px;background:var(--card);border:1px solid var(--line);
  padding:10px 10px 11px;position:relative;margin-right:18px}
.step:last-child{margin-right:0}
.step::after{content:"";position:absolute;right:-13px;top:50%;width:8px;height:8px;
  border-top:1px solid var(--line);border-right:1px solid var(--line);
  transform:translateY(-50%) rotate(45deg)}
.step:last-child::after{display:none}
.step b{display:block;font-size:12px;color:var(--accent);letter-spacing:.4px}
.step span{font-size:12.5px;color:var(--ink2);line-height:1.4;display:block;margin-top:2px}
.step.star{background:#fdf8f1;border-color:#e8d9c4}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px}
.kv dt{color:var(--ink2)}
.kv dd{margin:0}
.quote{background:var(--soft);border-left:3px solid #d8cec0;padding:8px 12px;
  font-size:12.5px;color:var(--ink2);white-space:pre-wrap;border-radius:0 6px 6px 0}
.pill{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.note{font-size:12.5px;color:var(--ink2)}
ul.tight{margin:6px 0 0;padding-left:18px}
ul.tight li{margin:3px 0}
.foot{margin-top:64px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--ink2);font-size:12px}
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _bars(scores: dict, total: int) -> str:
    rows = []
    for k, v in sorted(scores.items(), key=lambda kv: -kv[1]):
        pct = 0 if not total else v / total * 100
        rows.append(
            f'<tr><td>{_e(k)}</td><td class="num">{v}</td>'
            f'<td style="width:46%"><div class="bar"><i style="width:{pct:.1f}%"></i></div></td></tr>'
        )
    return f'<table><thead><tr><th>类别</th><th>块数</th><th>占比</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'


def _layout_block(lay: dict) -> str:
    fams = lay.get("family_scores") or {}
    best = max((v.get("score", 0) for v in fams.values()), default=1) or 1
    frows = []
    for k, v in sorted(fams.items(), key=lambda kv: -kv[1].get("score", 0)):
        pct = v.get("score", 0) / best * 100
        tag = '<span class="tag ok">启用</span>' if k in (lay.get("enabled_families") or []) else ""
        frows.append(
            f'<tr><td><code>{_e(k)}</code></td><td class="num">{v.get("hits", 0)}</td>'
            f'<td class="num">{v.get("recall", 0):.2f}</td>'
            f'<td style="width:34%"><div class="bar alt"><i style="width:{pct:.0f}%"></i></div></td>'
            f"<td>{tag}</td></tr>"
        )
    warns = "".join(f"<li>{_e(w)}</li>" for w in (lay.get("warnings") or []))
    notes = "".join(f"<li>{_e(n)}</li>" for n in (lay.get("notes") or []))
    return f"""
<div class="grid g2">
  <div class="card">
    <h3>测量结果</h3>
    <dl class="kv">
      <dt>文档类型</dt><dd><b>{_e(lay.get("doc_kind"))}</b> · 置信度 {_e(lay.get("confidence"))}</dd>
      <dt>版面</dt><dd>正文 {_e(lay.get("body_size"))}pt · {_e(lay.get("columns"))} 栏 · 左边界 {_e(lay.get("body_left"))}pt</dd>
      <dt>文字层</dt><dd>每页 {_e(lay.get("chars_per_page"))} 字 · 空白页 {lay.get("empty_page_ratio",0)*100:.0f}%</dd>
      <dt>书签</dt><dd>{_e(lay.get("bookmark_count"))} 条 {_e(lay.get("bookmark_levels"))}</dd>
      <dt>目录页</dt><dd>{_e(lay.get("toc_pages")) or "无"}（{_e(lay.get("toc_entry_count"))} 条）</dd>
      <dt>表格</dt><dd>{_e(lay.get("table_count"))} 张</dd>
      <dt>行文</dt><dd>引号行 {lay.get("dialog_ratio",0)*100:.1f}% · 骰子行 {lay.get("dice_ratio",0)*100:.1f}%</dd>
      <dt>页码偏移</dt><dd>PDF 页 − 印刷页 = {_e(lay.get("page_offset"))}</dd>
    </dl>
  </div>
  <div class="card">
    <h3>标题家族打分（书签即标注集）</h3>
    <table><thead><tr><th>家族</th><th>命中</th><th>召回</th><th>得分</th><th></th></tr></thead>
    <tbody>{"".join(frows)}</tbody></table>
    <p class="note">主策略 <code>{_e(lay.get("primary_family"))}</code>。启用家族可共存：
    “第X章”管一级、“1.2”管二级、“表Ⅰ：”管三级，它们不是竞争关系。</p>
  </div>
</div>
{"<div class=card style='margin-top:14px'><h3>探测记录</h3><ul class=tight>" + notes + "</ul></div>" if notes else ""}
{"<div class=card style='margin-top:14px'><h3>告警</h3><ul class=tight>" + warns + "</ul></div>" if warns else ""}
"""


def _field_table(fields: list[dict]) -> str:
    rows = []
    for f in fields[:60]:
        aliases = "、".join(f.get("aliases") or [])
        rows.append(
            f'<tr><td class="mono">{_e(f.get("key"))}</td><td>{_e(f.get("label"))}</td>'
            f'<td><span class="tag">{_e(f.get("kind"))}</span></td>'
            f'<td>{_e(f.get("unit") or "—")}</td><td class="num">{_e(f.get("first_page"))}</td>'
            f'<td>{_e(f.get("origin"))}</td>'
            f'<td class="quote" style="max-width:280px">{_e(f.get("quote"))}</td>'
            f'<td class="note">{_e(aliases)}</td></tr>'
        )
    return (
        "<table><thead><tr><th>字段 key</th><th>标签</th><th>类型</th><th>单位</th>"
        "<th>首次出现</th><th>来源</th><th>原文证据</th><th>别名</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _card_block(name: str, card: dict) -> str:
    if not card or card.get("error"):
        return ""

    def rows(items):
        return "".join(
            f'<tr><td>{_e(i["label"])}</td><td class="num">{_e(i["value"])}</td>'
            f'<td class="num note">{_e(i.get("page"))}</td></tr>'
            for i in items
        )

    src = card.get("source") or {}
    return f"""
<div class="card">
  <div style="display:flex;align-items:baseline;gap:10px">
    <h3 style="margin:0">{_e(card.get("name"))}</h3>
    <span class="tag">{_e(card.get("etype"))}</span>
    <span class="note">p{_e(card.get("page"))} · {_e(card.get("field_count"))} 个字段</span>
  </div>
  <table style="margin-top:10px"><thead><tr><th>字段</th><th>值</th><th>页</th></tr></thead>
  <tbody>{rows(card.get("structured", []))}{rows(card.get("labelled", []))}</tbody></table>
  {f'<div class="quote" style="margin-top:10px">{_e((src.get("text") or "")[:380])}</div>' if src else ''}
</div>"""


def _flow_block(f: dict | None) -> str:
    if not f:
        return '<p class="note">没有抽出流程图。</p>'
    nodes = f.get("nodes") or []
    steps = []
    for n in nodes:
        typ = n.get("type")
        cls = "step star" if typ == "decision" else "step"
        label = {"step": "步骤", "decision": "判定", "end": "结束"}.get(typ, typ)
        steps.append(f'<div class="{cls}"><b>{label}</b><span>{_e((n.get("text") or "")[:46])}</span></div>')
    return f'<div class="flow">{"".join(steps)}</div><p class="note">kind={_e(f.get("kind"))}；由编号步骤抽节点，“如果…则…”抽判定分支。置信度 0.55。</p>'


def render(report: dict, out_path: str | Path) -> Path:
    st = report.get("stats", {})
    q = report.get("quality", {})
    ex = report.get("extract", {})
    lay = report.get("layout", {})
    total_blocks = st.get("block", 0)

    kpis = [
        ("页", report.get("pages")),
        ("章节", st.get("section")),
        ("块", total_blocks),
        ("实体", st.get("entity")),
        ("字段（自动拓展）", st.get("field")),
        ("事实", st.get("fact")),
        ("流程图", st.get("flow")),
        ("耗时（秒）", report.get("total_seconds")),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="n">{_e(v)}</div><div class="l">{_e(k)}</div></div>' for k, v in kpis
    )

    steps = [
        ("S0 读入", "页 / 行 / 词", ""),
        ("S1 版式画像", "证据→假设→打分→定策", "star"),
        ("S2 粗分", "章节树", ""),
        ("S3 细分", "块（段落+条目头）", ""),
        ("S4 归类", "五大类+子类", ""),
        ("S5 抽取", "字段自动拓展", "star"),
        ("S6 建图", "流程/引用/场景", ""),
        ("S7 索引", "BM25+向量", ""),
        ("S8 自检", "覆盖率/可疑块", ""),
    ]
    flow_html = "".join(
        f'<div class="step {c}"><b>{_e(a)}</b><span>{_e(b)}</span></div>' for a, b, c in steps
    )

    samples = report.get("samples") or {}
    card_order = [("monster", "NPC 卡（只读）"), ("item", "物品卡"), ("skill", "技能卡"), ("rule", "规则条目")]
    cards_html = ""
    for etype, label in card_order:
        if etype in samples:
            cards_html += f'<div class="note" style="margin:18px 0 6px">{_e(label)}</div>' + _card_block(etype, samples[etype])

    audit = report.get("schema_audit") or []
    action_zh = {"new": "新增字段", "alias": "并入别名", "kind_fix": "修正类型", "new_etype": "新增实体类型"}
    audit_rows = "".join(
        f'<tr><td class="num">{_e(a.get("seq"))}</td><td><span class="tag">{_e(action_zh.get(a.get("action"), a.get("action")))}</span></td>'
        f'<td class="mono">{_e(a.get("field_key"))}</td><td>{_e(a.get("detail"))}</td>'
        f'<td class="num">{_e(a.get("page"))}</td></tr>'
        for a in audit[:12]
    )

    sp = report.get("spec") or {}
    spec_rows = "".join(
        f'<tr><td><b>{_e(k["zh"])}</b><br><span class="mono note">{_e(k["key"])}</span></td>'
        f'<td>{_e(k["boundary"])}</td><td class="note">{_e(k["evidence"])}</td>'
        f'<td class="note">{_e(k["payload"])}</td></tr>'
        for k in (sp.get("kinds") or [])
    )
    bs = report.get("bookshelf") or {}
    trace = bs.get("trace") or []
    bv = bs.get("verdict") or {}
    tr_rows = "".join(
        f'<tr><td class="mono">{_e(t.get("node"))}</td><td class="num">{_e(t.get("ms"))}</td>'
        f'<td>{_e(t.get("note"))}</td></tr>'
        for t in trace
    )
    vm = bv.get("metrics") or {}
    metric_rows = "".join(
        f'<tr><td class="mono">{_e(k)}</td><td class="num">{_e(v)}</td></tr>'
        for k, v in vm.items()
    )
    flow_line = " → ".join(dict.fromkeys([t.get("node") for t in trace if t.get("node")]))

    g = report.get("gate") or {}
    gsrc = g.get("source") or {}
    gate_rows = "".join(
        f'<tr><td><span class="tag warn">{_e(r.get("code"))}</span></td><td>{_e(r.get("scope"))}</td>'
        f'<td class="num">{_e(r.get("page"))}</td><td>{_e((r.get("title") or "")[:22])}</td>'
        f'<td class="note">{_e((r.get("detail") or "")[:60])}</td></tr>'
        for r in (g.get("log") or [])[:12]
    )
    violations = g.get("violations") or []

    sc = report.get("selfcheck") or {}
    sc_b, sc_a = sc.get("before") or {}, sc.get("after") or {}
    sc_rows = "".join(
        f'<tr><td>{_e(k)}</td><td class="num">{_e(sc_b.get(k))}</td>'
        f'<td class="num">{_e(sc_a.get(k))}</td></tr>'
        for k in ("blocks", "avg_len", "p95_len", "max_len", "over_long", "multi_entry", "fragments")
    )
    suspects = "".join(
        f'<li>p{_e(s.get("page"))} · {_e(s.get("title"))} —— 尾句「{_e(s.get("tail"))}」</li>'
        for s in (q.get("suspect_examples") or [])
    )

    body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>剧本编译报告 · {_e(report.get("title"))}</title><style>{CSS}</style></head>
<body><div class="wrap">

<h1>剧本编译报告</h1>
<p class="sub">《{_e(report.get("title"))}》 · {_e(report.get("pages"))} 页 ·
编译于 {_e(report.get("compiled_at"))} · 编译器 v0.1.0 · 产物 <code>data/kb/{_e(report.get("book_id"))}/kb.sqlite</code></p>

<div class="grid g4">{kpi_html}</div>

<h2>编译流水线</h2>
<div class="flow">{flow_html}</div>
<p class="note">两条星标是这套方案真正的差异化：<b>版式画像</b>决定"这本书怎么读"，
<b>字段自动拓展</b>决定"这本书有哪些字段"。两者都不预设，所以换书不用改代码。</p>

<h2>① 摸清版式（统一范式）</h2>
{_layout_block(lay)}

<h2>② 非游戏内容闸门 & 原文层</h2>
<div class="grid g4">
  <div class="kpi"><div class="n">{_e(g.get("denied_sections"))}</div><div class="l">整节被拒（目录/前言/译名表…）</div></div>
  <div class="kpi"><div class="n">{_e(g.get("denied_pages"))}</div><div class="l">整页被拒（版权页/空白页…）</div></div>
  <div class="kpi"><div class="n">{_e(gsrc.get("pages"))}</div><div class="l">原文层保留页数（仅游戏内容）</div></div>
  <div class="kpi"><div class="n">{_e(gsrc.get("chars"))}</div><div class="l">原文层字数（逐页原样）</div></div>
</div>
<div class="card" style="margin-top:14px">
  <p class="note" style="margin-top:0">
  <b>强约束，不是关键词过滤。</b>三条机制：① 结构层先按书签/目录拒整节；
  ② 页级再判版权页、名单页、链接页（判不准就拒，fail-closed）；
  ③ 编译末尾跑断言，在 <code>block.text / entity.name / fact.raw</code> 里反查版权与网址特征，
  命中数必须为 0。被拒的内容只留「页码 + 理由 + 指纹」，正文一个字都不进库。</p>
  <div class="pill">
    <span class="tag {'ok' if not violations else 'warn'}">知识层违规 {len(violations)} 处</span>
    <span class="tag">原因分布 {_e(g.get("by_code"))}</span>
  </div>
  <table style="margin-top:12px"><thead><tr><th>原因</th><th>范围</th><th>页</th><th>标题</th><th>说明</th></tr></thead>
  <tbody>{gate_rows}</tbody></table>
  <p class="note">原文层：<code>source_page</code> 表逐页保存原样文本（阅读顺序、不摊平表格），
  块用 <code>src_page/src_start/src_end</code> 指回它 —— <code>cli raw --page 260</code> 看某页，
  <code>cli export --out 全文.md</code> 导出人读版。</p>
</div>

<h2>③ 五大类分布</h2>
<div class="grid g2">
  <div class="card">
    {_bars(st.get("by_kind") or {}, total_blocks)}
    <p class="note">判定顺序：章节先验（权重 5）→ 词汇特征（封顶）→ 版式签名（最硬）。
    子类由内容里的领域词决定，读到新领域词就并排新增一个子类。</p>
  </div>
  <div class="card">
    <h3>实体类型（也在生长）</h3>
    {_bars(st.get("by_etype") or {}, st.get("entity") or 1)}
    <p class="note">抽取次数：{_e(ex)}。同名实体自动归并，所以"抽取次数 &gt; 实体数"是正常的
    （技能索引页和技能列表页各出现一次）。</p>
  </div>
</div>

<h2>④ 正文规范：分类的硬约束</h2>
<div class="card">
  <div class="pill" style="margin:0 0 8px">
    <span class="tag {'ok' if sp.get('compliance', 0) > 0.95 else 'warn'}">合规率 {sp.get("compliance", 0) * 100:.1f}%</span>
    <span class="tag">检查 {_e(sp.get("checked"))} 块</span>
    <span class="tag">不合规 {_e(sp.get("violation_count"))}</span>
    <span class="tag">未分类 {_e(sp.get("unclassified_count"))}</span>
  </div>
  <p class="note" style="margin:0 0 10px">分类不是"模型看着办"：先有这份规范，规则分类器按它判，LLM 按它答，报告按它验。
  每类都写清<b>边界 / 证据 / 禁止 / 必须产出的结构化内容</b>；候选按分数排序后逐条过规范，
  一条都过不了就是 <code>unclassified</code> —— 宁可留空，不要硬塞。</p>
  <table><thead><tr><th>类别</th><th>块边界</th><th>证据（至少一条）</th><th>必须产出</th></tr></thead>
  <tbody>{spec_rows}</tbody></table>
</div>

<h2>⑤ 字段字典：表结构是读出来的，不是设计出来的</h2>
<div class="card">
  <p class="note" style="margin-top:0">
  这本书里没有任何"字段表"。下面每一行都是 Agent 在读到某个表头 / 标签行 / 正文内嵌值时现场登记的，
  带页码与原文证据（<code>evidence_quote</code>）。类型由<b>值</b>推出，不由标签猜：
  <code>1D6+1</code>→dice、<code>95</code>→num、<code>稀/CP/C</code>→enum、<code>见6.6</code>→ref。
  同义写法（价格/花费/售价）合并成一条字段的不同别名，而不是长成三列。</p>
  {_field_table(report.get("field_dict") or [])}
  <p class="note">共 {_e(st.get("field"))} 个字段，按 scope：{_e(st.get("by_scope"))}。上表只列前 60 条。</p>
</div>

<h3>字段拓展审计流（最近 12 条）</h3>
<div class="card"><table><thead><tr><th>#</th><th>动作</th><th>字段</th><th>详情</th><th>页</th></tr></thead>
<tbody>{audit_rows}</tbody></table></div>

<h2>⑥ 抽取样例</h2>
<div class="grid g2">{cards_html}</div>

<h2>⑦ 规则流程图</h2>
<div class="card">{_flow_block(report.get("flow_sample"))}</div>

<h2>⑧ 质量自检</h2>
<div class="card" style="margin-bottom:14px">
  <div class="pill" style="margin:0 0 8px">
    <span class="tag {'ok' if sc.get('passed') else 'warn'}">自检 {_e(sc.get('rounds'))} 轮 · {'通过' if sc.get('passed') else '未通过'}</span>
    <span class="tag">残留 {sc.get('residual_rate', 0) * 100:.2f}%</span>
    <span class="tag">修复动作 {_e(len(sc.get("actions") or []))} 条</span>
  </div>
  <p class="note" style="margin:0 0 8px">切完自己量一遍：超长、一块多条、碎片、断名、句子被切断。
  不合格就自己重切，最多 4 轮，每轮动作都留痕（<code>report.json → selfcheck.actions</code>）。</p>
  <table><thead><tr><th>指标</th><th>修复前</th><th>修复后</th></tr></thead><tbody>{sc_rows}</tbody></table>
</div>
<div class="grid g3">
  <div class="kpi"><div class="n">{_e(q.get("pages_without_block_count"))}</div><div class="l">页没有产出块（多为插图/空白页）</div></div>
  <div class="kpi"><div class="n">{_e(q.get("suspect_blocks"))}</div><div class="l">可疑块（尾句无句末标点 = 跨页断句）</div></div>
  <div class="kpi"><div class="n">{_e(q.get("low_confidence_blocks"))}</div><div class="l">低置信块（&lt;0.45，建议复核）</div></div>
</div>
<div class="card" style="margin-top:14px">
  <h3>可疑块样例</h3><ul class="tight">{suspects}</ul>
  <h3>已知缺陷</h3>
  <ul class="tight">
    <li>怪物只抽到 20 只（第十四章实际 60+）：部分属性块不是三列表，需要第二种锚点。</li>
    <li>物品价格偶有错列：表头被合并单元格时按词贴列会偏移。</li>
    <li>规则流程图只覆盖 7 条：目前只认编号步骤，未认"若…则…"分支句式。</li>
    <li>跨页断句 24 处：块尾与下一页首行尚未续接。</li>
  </ul>
  <p class="note">这些不是"以后再说"，而是每次编译自动重算的指标，写在 <code>report.json</code> 的 <code>quality</code> 段里。</p>
</div>

<h2>⑨ 存储结构</h2>
<div class="card">
<pre class="mono" style="margin:0;white-space:pre-wrap;color:#3b3733">source_page 原文层   逐页原样文本（只存游戏内容页）                ★ 便于人查看/校对
non_game_log 拦截台账  页码 + 理由 + 指纹（不留正文）
entity  实体       拜亚基 · 考古学 · 燧发手枪 · 战斗轮            （知识，只读）
fact    事实       (实体, 字段, 值, 引用, 页码)                     （值永远带证据）
field   字段字典   monster.STR · item.伤害 · spell.消耗             ★ Agent 产出
edge    关系       section → section（"见第143页"这类交叉引用）
flow    流程图     rule → 节点/边
--- 以上是知识轴（静态） · 以下是时间轴（动态） ---
campaign      团：当前场景 / 世界时间 / 游标
timeline_event 只追加的事件流（可回放）
entity_state   实体在本次团里的状态（真相源，死者留墓碑）
block_fts      BM25 全文索引（jieba 预切词）
block_vec      bge-small-zh 向量（可选，缺了就退化成纯 BM25）</pre>
</div>

<h2>⑩ 编排：LangGraph 图与自证</h2>
<div class="card" style="margin-bottom:14px">
  <div class="pill" style="margin:0 0 8px">
    <span class="tag {'ok' if bv.get('passed') else 'warn'}">{'自证通过' if bv.get('passed') else '自证未通过'}</span>
    <span class="tag">节点 {_e(len(trace))} 步</span>
    <span class="tag">编排列 {_e(bs.get('graph') or '—')}</span>
  </div>
  <p class="note" style="margin:0 0 8px">整条流水线是一张 <code>StateGraph</code>：状态里只放可序列化的事实，
  重资产（PDF 的 bbox、数据库连接）按路径重建。所以每个节点都可以单独重跑，整本书也可以从中断处续跑。</p>
  <p class="mono" style="font-size:12px;margin:0 0 10px;color:#3b3733">{_e(flow_line)}</p>
  <table><thead><tr><th>节点</th><th>耗时(ms)</th><th>做了什么</th></tr></thead><tbody>{tr_rows}</tbody></table>
</div>
<div class="card">
  <p class="note" style="margin:0 0 8px">自证的判据（四条全过才算编译成功，任何一条不过都要说清是哪条）：
  闸门零违规 · 切分自检通过 · 规范合规率达标 · 未分类占比不超标。</p>
  <table><thead><tr><th>指标</th><th>值</th></tr></thead><tbody>{metric_rows}</tbody></table>
  <p class="note" style="margin:8px 0 0">结论：{_e("；".join(bv.get("reasons") or []))}</p>
</div>

<div class="foot">compiler v0.1.0 · 《{_e(report.get("title"))}》 ·
{_e(st.get("section"))} sections / {_e(total_blocks)} blocks / {_e(st.get("entity"))} entities ·
耗时 {_e(report.get("total_seconds"))}s</div>
</div></body></html>"""

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    return out
