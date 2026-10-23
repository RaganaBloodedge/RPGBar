"""把知识库渲染成一个**可翻阅的页面**：切分后的块 + 原文层，两栏可切。

为什么要单独做这个页面：
- 命令行看 `block.text` 要写 SQL，翻 900 多块不现实；
- 编译报告只给统计和抽样，看不到"第 47 页到底被切成了几块、每块长什么样"；
- 校对时最需要的动作是"顺着页码往下读"，HTML 天然合适。

页面上每一块都标了：出自哪一章（section path）、哪一页、什么类别、字符数、以及它在
`source_page` 里的字符区间 —— 点页码能看到该页原文（未切分的样子）。
"""
from __future__ import annotations

import html
import sqlite3
from pathlib import Path

KIND_ZH = {
    "rule": "规则",
    "pc": "玩家属性",
    "tool": "工具",
    "npc": "NPC",
    "lore": "常识世界观",
}

CSS = """
:root{--bg:#fbfaf8;--card:#fff;--ink:#1c1b1a;--ink2:#5c5852;--line:#e6e2db;
--a:#8a5a2b;--a2:#2f6b5f;--soft:#f4f0e9;--k-rule:#8a5a2b;--k-pc:#2f6b5f;
--k-tool:#6a5a8a;--k-npc:#a8501f;--k-lore:#4a6b8a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.7 "PingFang SC","Microsoft YaHei",-apple-system,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:9;background:rgba(251,250,248,.96);
border-bottom:1px solid var(--line);backdrop-filter:blur(6px)}
.hd{max-width:1180px;margin:0 auto;padding:14px 28px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
h1{font-size:16px;font-weight:600;margin:0;letter-spacing:-.2px}
.meta{color:var(--ink2);font-size:12.5px}
.tools{display:flex;gap:8px;align-items:center;margin-left:auto;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--card);border-radius:999px;
padding:3px 11px;font-size:12px;cursor:pointer;user-select:none;color:var(--ink2)}
.chip.on{background:var(--ink);color:#fff;border-color:var(--ink)}
input[type=search]{border:1px solid var(--line);border-radius:999px;padding:5px 12px;
font-size:12.5px;width:200px;outline:none;background:var(--card)}
input[type=search]:focus{border-color:var(--a)}
main{max-width:1180px;margin:0 auto;padding:26px 28px 90px}
.chap{margin:34px 0 10px;font-size:13px;color:var(--ink2);font-weight:600;
letter-spacing:.3px;border-left:3px solid var(--a);padding-left:9px}
.blk{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:13px 16px 14px;margin:10px 0}
.blk h3{margin:0 0 4px;font-size:14.5px;font-weight:600;display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.badge{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);
color:var(--ink2);background:var(--soft);font-weight:400}
.b-rule{color:#fff;background:var(--k-rule);border-color:var(--k-rule)}
.b-pc{color:#fff;background:var(--k-pc);border-color:var(--k-pc)}
.b-tool{color:#fff;background:var(--k-tool);border-color:var(--k-tool)}
.b-npc{color:#fff;background:var(--k-npc);border-color:var(--k-npc)}
.b-lore{color:#fff;background:var(--k-lore);border-color:var(--k-lore)}
.tx{white-space:pre-wrap;word-break:break-word;margin:8px 0 0;
font-size:13.2px;line-height:1.78;color:#2b2926;
max-height:340px;overflow:auto;padding-right:6px}
.tx.full{max-height:none}
.more{font-size:12px;color:var(--a);cursor:pointer;margin-top:4px;display:inline-block}
.pg{font-size:12px;color:var(--ink2)}
.pg a{color:var(--a);text-decoration:none;border-bottom:1px dotted var(--a)}
.empty{color:var(--ink2);font-size:13px;padding:40px 0;text-align:center}
details.pgbox{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:10px 14px;margin:8px 0}
details.pgbox summary{cursor:pointer;font-size:13px;color:var(--ink2)}
details.pgbox pre{white-space:pre-wrap;word-break:break-word;font:13.2px/1.8 inherit;
margin:10px 0 0;color:#2b2926}
.hidden{display:none}
.legend{font-size:12px;color:var(--ink2);margin:10px 0 0}
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def render(db_path: str | Path, out_path: str | Path) -> Path:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    book = con.execute("SELECT * FROM book").fetchone()
    blocks = con.execute("SELECT * FROM block ORDER BY page, ord").fetchall()
    pages = con.execute("SELECT page, chars, text FROM source_page ORDER BY page").fetchall()

    parts: list[str] = []
    cur_chap = None
    for b in blocks:
        path = con.execute("SELECT path FROM section WHERE id=?", (b["section_id"],)).fetchone()
        chap = (path["path"] if path else "") or "（无章节）"
        if chap != cur_chap:
            # 章节层级太细，按"第一个 >"分组，读起来像目录
            parts.append(f'<div class="chap">{_e(chap)}</div>')
            cur_chap = chap
        kind = b["kind"] or ""
        src = ""
        if b["src_start"] is not None:
            src = f' · 原文 {b["src_start"]}–{b["src_end"]}'
        parts.append(
            f'<div class="blk" data-kind="{_e(kind)}" data-page="{b["page"]}">'
            f'<h3>{_e(b["title"])}'
            f'<span class="badge b-{_e(kind)}">{_e(KIND_ZH.get(kind, kind or "未分类"))}·{_e(b["subtype"] or "-")}</span>'
            f'<span class="pg"><a href="#p{b["page"]}">p{b["page"]}</a> · {b["char_len"]} 字 · 置信 {b["confidence"]:.2f}{_e(src)}</span>'
            f'</h3>'
            f'<div class="tx">{_e(b["text"])}</div>'
            f'</div>'
        )

    page_box = []
    for p in pages:
        page_box.append(
            f'<details class="pgbox" id="p{p["page"]}" data-page="{p["page"]}">'
            f'<summary>原文 p{p["page"]}（{p["chars"]} 字，未切分）</summary>'
            f'<pre>{_e(p["text"])}</pre></details>'
        )

    body = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>切分结果 · {_e(book["title"])}</title><style>{CSS}</style></head><body>
<header><div class="hd">
  <h1>{_e(book["title"])} · 切分结果</h1>
  <span class="meta">{len(blocks)} 块 · {len(pages)} 页原文 · {book["pages"]} 页 PDF</span>
  <div class="tools">
    <span class="chip on" data-f="all">全部</span>
    <span class="chip" data-f="rule">规则</span>
    <span class="chip" data-f="pc">玩家属性</span>
    <span class="chip" data-f="tool">工具</span>
    <span class="chip" data-f="npc">NPC</span>
    <span class="chip" data-f="lore">常识世界观</span>
    <input type="search" id="q" placeholder="搜标题或正文…">
  </div>
</div></header>
<main>
<p class="legend">每块显示：标题 + 类别/子类 + 页码 + 字数 + 原文区间（点击页码跳到该页原文）。
文字即原文，未经改写；表格在块里被摊平成“a | b | c”，整页原文在页面底部。</p>
<div id="list">{"".join(parts)}</div>

<h1 style="margin:44px 0 6px;font-size:16px">原文层（整页，未切分）</h1>
<p class="legend">这是 <code>source_page</code> 表的内容：编译时从 PDF 里读出来的逐页文本，
用于校对“块切得对不对”。点开任意一页对照上面的块。</p>
{"".join(page_box)}
<div class="empty" id="none" style="display:none">没有匹配的块</div>
</main>
<script>
const chips=[...document.querySelectorAll('.chip')], q=document.getElementById('q'),
      blks=[...document.querySelectorAll('.blk')], none=document.getElementById('none');
let filter='all';
function apply(){{
  const kw=q.value.trim(); let n=0;
  for(const b of blks){{
    const okK = filter==='all' || b.dataset.kind===filter;
    const okQ = !kw || b.textContent.includes(kw);
    const show = okK && okQ;
    b.classList.toggle('hidden', !show); if(show) n++;
  }}
  none.style.display = n? 'none':'block';
}}
chips.forEach(c=>c.onclick=()=>{{chips.forEach(x=>x.classList.remove('on'));
  c.classList.add('on'); filter=c.dataset.f; apply();}});
q.oninput=apply;
// 点正文可展开/收起（长块默认给个滚动区，读整条时展开）
blks.forEach(b=>b.querySelector('.tx').onclick=ev=>{{
  if(ev.target.tagName==='A') return; ev.currentTarget.classList.toggle('full');}});
</script></body></html>"""

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    con.close()
    return out
