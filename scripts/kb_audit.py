"""知识库切分质量审计：把「交接文档 §6 的四条验收」变成可重复跑的数字。

用法：
    python scripts/kb_audit.py --kb data/kb/coc7e-llm/kb.sqlite [--json out.json]

它只读库，不改任何东西。每条指标都对应一个**已知的失败模式**（见 handoff §4），
所以"修好了没有"不靠肉眼，靠这几行数字：

    dup_same_page        同页高度重复的块（三胞胎/跨章重复切同一页）
    same_title_groups    同标题 ≥3 块的组数（5.7 对抗检定那种）
    cont_fragments       `-s1` 续块碎片
    orphan_stat_rows     标题是 STR/CON/... 的属性表行块（属性表脱离怪物的证据）
    stat_blocks          带 meta.statblock 的条目块（属性表已并入条目的证据）
    pseudo_entities      被抽成实体的属性缩写（INT/POW 伪实体）
    deepone_*            深潜者档案：fact 数 / CON 取值数 / 来源块数
    canonical_false      canonical=false 的块（示例/演示/作者注）
    unclassified         未分类块
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict

ABBR = {"STR", "CON", "SIZ", "DEX", "INT", "POW", "APP", "EDU", "LUK", "LUCK"}
EXAMPLE_WORDS = ("例如", "示例", "比如", "举例", "演示", "仅供参考")


def _flat(s: str) -> str:
    return re.sub(r"[\s　]+", "", s or "")


def audit(kb: str, deepone: str = "深潜者") -> dict:
    con = sqlite3.connect(kb)
    con.row_factory = sqlite3.Row
    c = con.cursor()

    blocks = [dict(r) for r in c.execute(
        "SELECT id, section_id, page, page_end, title, text, kind, char_len, meta FROM block"
    )]
    out: dict = {}
    out["blocks"] = len(blocks)
    out["by_kind"] = dict(c.execute(
        "SELECT kind, COUNT(*) FROM block GROUP BY kind ORDER BY 2 DESC").fetchall())
    out["avg_len"] = round(sum(b["char_len"] or 0 for b in blocks) / max(1, len(blocks)), 1)
    out["unclassified"] = out["by_kind"].get("unclassified", 0)

    # 同页重复：同 page + 同 section 不算，**跨 section 的重复才是重复切页**
    seen: dict[tuple, list[str]] = defaultdict(list)
    for b in blocks:
        key = (b["page"], _flat((b["text"] or ""))[:400])
        if len(key[1]) >= 40:
            seen[key].append(b["id"])
    dup_groups = {k: v for k, v in seen.items() if len(v) > 1}
    # 同一 section 内允许（那是自检切出来的），跨 section 才是问题
    sec_of = {b["id"]: b["section_id"] for b in blocks}
    cross = {k: v for k, v in dup_groups.items() if len({sec_of[i] for i in v}) > 1}
    out["dup_same_page"] = len(dup_groups)
    out["dup_cross_section"] = len(cross)
    out["dup_blocks"] = sum(len(v) - 1 for v in dup_groups.values())

    title_groups = defaultdict(list)
    for b in blocks:
        t = _flat(b["title"] or "")
        if t:
            title_groups[t].append(b["id"])
    same3 = {t: v for t, v in title_groups.items() if len(v) >= 3}
    out["same_title_groups"] = len(same3)
    out["same_title_blocks"] = sum(len(v) for v in same3.values())
    out["same_title_top"] = sorted(
        ((t, len(v)) for t, v in same3.items()), key=lambda x: -x[1])[:8]

    out["cont_fragments"] = sum(1 for b in blocks if re.search(r"-s\d+$", b["id"]))
    out["cont_fragment_titles"] = Counter(
        (b["title"] or "")[:20] for b in blocks if re.search(r"-s\d+$", b["id"])).most_common(5)

    # 属性表行块：标题是属性缩写、meta.is_table、字符数很短
    orphan = [b for b in blocks if (b["title"] or "").strip().upper() in ABBR
              and (b["meta"] or "").find('"is_table": true') >= 0]
    out["orphan_stat_rows"] = len(orphan)
    out["orphan_stat_row_sample"] = [b["id"] for b in orphan[:6]]

    stat_blocks = 0
    for b in blocks:
        if '"statblock"' in (b["meta"] or ""):
            stat_blocks += 1
    out["stat_blocks"] = stat_blocks

    # 伪实体：名字是属性缩写
    pseudo = [dict(r) for r in c.execute(
        "SELECT name, etype, block_id FROM entity WHERE UPPER(name) IN (%s)"
        % ",".join("?" * len(ABBR)), tuple(ABBR))]
    out["pseudo_entities"] = len(pseudo)
    out["pseudo_samples"] = [f"{p['name']}:{p['etype']}" for p in pseudo[:10]]

    # 深潜者档案
    row = c.execute("SELECT id FROM entity WHERE name=?", (deepone,)).fetchone()
    if row:
        eid = row["id"]
        facts = [dict(r) for r in c.execute(
            "SELECT field_key, raw, block_id FROM fact WHERE entity_id=?", (eid,))]
        cons = [f for f in facts if f["field_key"] in ("monster.CON", "npc.CON")]
        out["deepone_facts"] = len(facts)
        out["deepone_fields"] = len({f["field_key"] for f in facts})
        out["deepone_con_values"] = sorted({str(f["raw"]) for f in cons})
        out["deepone_blocks"] = sorted({f["block_id"] or "" for f in facts})
        out["deepone_sample"] = [f"{f['field_key']}={f['raw']}" for f in facts[:10]]
    else:
        out["deepone_facts"] = 0

    # canonical
    canon = Counter()
    for b in blocks:
        m = b["meta"] or ""
        canon["false" if '"_canonical": false' in m or '"canonical": false' in m else "true"] += 1
    out["canonical_false"] = canon.get("false", 0)
    out["canonical_true"] = canon.get("true", 0)
    ex = [b for b in blocks if any(w in (b["text"] or "")[:120] for w in EXAMPLE_WORDS)]
    out["example_blocks"] = len(ex)
    out["example_kind"] = dict(Counter(b["kind"] for b in ex).most_common(6))

    out["entities"] = dict(c.execute(
        "SELECT etype, COUNT(*) FROM entity GROUP BY etype ORDER BY 2 DESC").fetchall())
    out["facts"] = c.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    out["sections"] = c.execute("SELECT COUNT(*) FROM section").fetchone()[0]

    # 同页多章节（三胞胎的病根：书签同页 + page_end 截断）
    shares = c.execute(
        "SELECT page_start, COUNT(*) c FROM section GROUP BY page_start HAVING c>1"
    ).fetchall()
    out["pages_with_multi_sections"] = len(shares)
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True)
    ap.add_argument("--json", default="")
    ap.add_argument("--deepone", default="深潜者")
    args = ap.parse_args()
    out = audit(args.kb, args.deepone)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
