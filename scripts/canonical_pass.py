"""补标 canonical（示例/演示 → false）：对**看起来像示例**的块单独问模型一句。

为什么单独做一遍：canonical 和"这块属于五类里的哪一类"是两个正交问题，混在一次
提问里 4B 会一律回 true（实测「1.3 游戏范例」整段被当成正典）。拆成二选一的问题
它就能答对；而且只问可疑的块，成本可以忽略。

用法：python scripts/canonical_pass.py --kb data/kb/coc7e-v2/kb.sqlite [--dry]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, r"E:\Workspace\RPGBar")
os.environ.setdefault("RPGBAR_LLM", "qwen3-4b@http://127.0.0.1:8080/v1|sk-local")

from compiler.graph.judge import Judge  # noqa: E402

_DEMO_CUES = ("例如", "示例", "范例", "举例", "比如", "演示", "实录", "例子")


def suspect(title: str, section: str, text: str) -> bool:
    if any(c in section or c in title for c in _DEMO_CUES):
        return True
    if len(re.findall(r"^[^\n：:]{1,8}[：:]\s", text or "", re.M)) >= 3:
        return True
    return any(c in (text or "")[:240] for c in _DEMO_CUES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(args.kb)
    con.row_factory = sqlite3.Row
    rows = []
    for r in con.execute(
        "SELECT b.id,b.title,b.text,b.meta,s.path FROM block b LEFT JOIN section s ON s.id=b.section_id"
    ):
        if suspect(r["title"] or "", r["path"] or "", r["text"] or ""):
            rows.append(dict(r))
    print(f"可疑块 {len(rows)} 个")
    judge = Judge(batch=1, workers=1)
    if not judge.available:
        print("没有模型端点")
        return 1
    flags = judge.is_demo([(r["id"], r["title"] or "", r["text"] or "") for r in rows])
    n = 0
    for r in rows:
        if flags.get(r["id"]) is not True:
            continue
        meta = json.loads(r["meta"] or "{}")
        if meta.get("canonical") is False:
            continue
        meta["canonical"] = False
        n += 1
        print(f"  false: {r['id']} {r['title'][:30]!r}")
        if not args.dry:
            con.execute("UPDATE block SET meta=?, canonical=0 WHERE id=?",
                        (json.dumps(meta, ensure_ascii=False), r["id"]))
    if not args.dry:
        con.commit()
    print(f"标记 {n} 块 canonical=false（问过 {len(rows)} 块）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
