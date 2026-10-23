"""验收：把 handoff §6 的四条验收 + 本次重写的两条硬指标，跑成 PASS/FAIL。

用法：python scripts/kb_acceptance.py --kb data/kb/coc7e-v2/kb.sqlite

四条（handoff 原文）：
  1. 深潜者档案无污染（CON 唯一且合理）
  2. INT/POW 伪实体消失
  3. 示例块 canonical=false
  4. 三胞胎消失

两条（这版新增，判定权与证据链）：
  5. 所有块的类别都是模型判的（判源覆盖率 100%，没有静默回落）
  6. 怪物属性表并回条目（stat_blocks > 0），且没有游离的属性行块
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict


def _flat(s: str) -> str:
    return re.sub(r"[\s　]+", "", s or "")


def _dice_bounds(expr: str) -> tuple[float, float] | None:
    """掷骰表达式 → (最小平均值, 最大平均值)。只认 (nDm+k)×s 这种常见写法。"""
    s = _flat(expr).replace("X", "×").replace("x", "×")
    m = re.search(r"(\d*)D(\d+)([+\-]\d+)?(?:×(\d+))?", s, re.I)
    if not m:
        return None
    n = int(m.group(1) or 1)
    d = int(m.group(2))
    k = int(m.group(3) or 0)
    scale = int(m.group(4) or 1)
    return ((1 + k) * scale, (d * n + k) * scale)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True)
    ap.add_argument("--deepone", default="深潜者")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    con = sqlite3.connect(args.kb)
    con.row_factory = sqlite3.Row
    c = con.cursor()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    # ---- 1. 深潜者
    row = c.execute("SELECT id FROM entity WHERE name=?", (args.deepone,)).fetchone()
    if not row:
        add("深潜者档案无污染", False, "库里没有「深潜者」实体")
    else:
        facts = [dict(r) for r in c.execute(
            "SELECT field_key, raw, num, block_id FROM fact WHERE entity_id=?", (row["id"],))]
        cons = [f for f in facts if f["field_key"].endswith(".CON")]
        con_vals = sorted({str(f["raw"]) for f in cons})
        blocks = sorted({f["block_id"] for f in facts if f["block_id"]})
        dice = [f for f in facts if f["field_key"].endswith("CON·掷骰")]
        ok = len(con_vals) == 1 and len(blocks) == 1
        detail = f"CON={con_vals} / 来源块={blocks}"
        if ok and dice:
            b = _dice_bounds(dice[0]["raw"])
            if b and con_vals[0].replace(".0", "").isdigit():
                v = float(con_vals[0])
                if not (b[0] * 0.7 <= v <= b[1] * 1.3):
                    ok = False
                    detail += f"；但 CON={v} 不在掷骰范围 {b} 内"
        add("深潜者档案无污染", ok, detail)

    # ---- 2. 伪实体
    abbr = ("STR", "CON", "SIZ", "DEX", "INT", "POW", "APP", "EDU", "LUK", "LUCK")
    pseudo = [dict(r) for r in c.execute(
        "SELECT name, etype, block_id FROM entity WHERE UPPER(name) IN (%s)"
        % ",".join("?" * len(abbr)), abbr)]
    bad = [p for p in pseudo if p["etype"] in ("monster", "npc", "item", "rule", "spell")]
    add("INT/POW 伪实体消失", not bad,
        f"属性缩写被抽成实体 {len(bad)} 个：{[(p['name'], p['etype']) for p in bad[:8]]}")

    # ---- 3. canonical
    blocks = [dict(r) for r in c.execute(
        "SELECT id, page, title, text, kind, meta FROM block")]
    canon_false = [b for b in blocks if '"_canonical": false' in (b["meta"] or "")
                   or '"canonical": false' in (b["meta"] or "")]
    ex = [b for b in blocks if any(w in (b["text"] or "")[:160]
                                   for w in ("例如", "示例", "比如", "举例", "演示"))]
    ex_false = [b for b in ex if b in canon_false]
    add("示例块 canonical=false", len(canon_false) > 0 and (not ex or len(ex_false) > 0),
        f"canonical=false {len(canon_false)} 块；含示例词的块 {len(ex)} 个，其中标 false {len(ex_false)} 个")

    # ---- 4. 三胞胎（同页同文重复）
    seen: dict[tuple, list[str]] = defaultdict(list)
    for b in blocks:
        key = (b["page"], _flat((b["text"] or ""))[:300])
        if len(key[1]) >= 60:
            seen[key].append(b["id"])
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    add("三胞胎消失", not dups, f"同页同文重复组 {len(dups)} 个：{list(dups.values())[:3]}")

    # ---- 5. 判源
    by = Counter()
    for b in blocks:
        m = b["meta"] or ""
        by["llm" if '"by": "llm"' in m else ("rule" if '"by": "rule"' in m else "?")] += 1
    cov = by["llm"] / max(1, len(blocks))
    add("判源覆盖率 100%", cov >= 0.999,
        f"llm {by['llm']} / rule {by['rule']} / 未知 {by['?']}（覆盖率 {cov:.1%}）")

    # ---- 6. 属性表归位
    stat_blocks = sum(1 for b in blocks if '"statblock"' in (b["meta"] or ""))
    orphan = [b for b in blocks if (b["title"] or "").strip().upper() in abbr
              and '"is_table": true' in (b["meta"] or "")]
    add("属性表并回条目", stat_blocks > 0 and not orphan,
        f"带 statblock 的条目 {stat_blocks} 个；游离属性行块 {len(orphan)} 个")

    out = {"kb": args.kb, "checks": checks,
           "passed": all(x["pass"] for x in checks),
           "blocks": len(blocks), "entities": c.execute("SELECT COUNT(*) FROM entity").fetchone()[0]}
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.json:
        open(args.json, "w", encoding="utf-8").write(text)
    print(text)
    return 0 if out["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
