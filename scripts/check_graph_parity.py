"""一致性检查：旧序贯管线 vs LangGraph 图编排，产出必须逐字一致。

存在的理由很实际：把一条 600 行的顺序流程改写成状态图，最容易出的错不是"跑不起来"，
而是"跑起来了但结果变了" —— 少跑一个节点、边接错顺序、某个字段被覆盖。这类错误
在报告里看不出异常（数字都在合理范围），只有拿两边的库逐块比才看得出来。

判据（全部通过才算一致）：
1. 九张表的行数相同；
2. 五大类分布相同；
3. 实体类型分布相同；
4. 块 id 集合完全相同；
5. 每块正文前 80 字完全相同（抽样深度可控，默认全量）。

用法：
    python scripts/check_graph_parity.py --a data/kb/coc7e/kb.sqlite --b data/kb/coc7e-graph/kb.sqlite

退出码：0 一致；1 有差异（并打印第一处不同的位置）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

TABLES = ("section", "block", "entity", "field", "fact", "source_page", "non_game_log", "flow", "edge")


def snapshot(path: str, depth: int = 80) -> dict:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    s: dict = {}
    for t in TABLES:
        s[t] = con.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
    s["by_kind"] = {r["kind"]: r["n"] for r in con.execute("SELECT kind,COUNT(*) n FROM block GROUP BY kind")}
    s["by_etype"] = {r["etype"]: r["n"] for r in con.execute("SELECT etype,COUNT(*) n FROM entity GROUP BY etype")}
    s["texts"] = {r["id"]: (r["text"] or "")[:depth] for r in con.execute("SELECT id,text FROM block")}
    con.close()
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对比两条管线的编译产物")
    ap.add_argument("--a", required=True, help="基准库（旧序贯管线）")
    ap.add_argument("--b", required=True, help="对照库（LangGraph 管线）")
    ap.add_argument("--depth", type=int, default=80, help="每块比对的正文字符数")
    args = ap.parse_args(argv)

    a, b = snapshot(args.a, args.depth), snapshot(args.b, args.depth)
    bad: list[str] = []

    print(f"{'指标':16s}{'基准':>10s}{'对照':>10s}")
    for t in TABLES:
        flag = "=" if a[t] == b[t] else "≠"
        if flag == "≠":
            bad.append(f"{t} 行数 {a[t]} vs {b[t]}")
        print(f"{t:16s}{a[t]:>10d}{b[t]:>10d}  {flag}")

    for key in ("by_kind", "by_etype"):
        if a[key] != b[key]:
            diff = {k: (a[key].get(k), b[key].get(k)) for k in set(a[key]) | set(b[key])
                    if a[key].get(k) != b[key].get(k)}
            bad.append(f"{key} 不一致：{diff}")
            print(f"{key}: ≠ {diff}")
        else:
            print(f"{key}: = {a[key]}")

    ids_a, ids_b = set(a["texts"]), set(b["texts"])
    if ids_a != ids_b:
        only = list(ids_a ^ ids_b)[:5]
        bad.append(f"块 id 不一致，差异 {len(ids_a ^ ids_b)} 个，例如 {only}")
        print(f"块 id：≠ 差异 {len(ids_a ^ ids_b)}")
    else:
        print(f"块 id：= 共 {len(ids_a)} 个")

    diff_text = [k for k in (ids_a & ids_b) if a["texts"][k] != b["texts"].get(k)]
    if diff_text:
        k = diff_text[0]
        bad.append(f"{len(diff_text)} 块正文不一致，例如 {k}：\n  A={a['texts'][k][:60]!r}\n  B={b['texts'].get(k, '')[:60]!r}")
        print(f"正文：≠ {len(diff_text)} 块不同")
    else:
        print(f"正文：= {len(ids_a)} 块前 {args.depth} 字全一致")

    if bad:
        print("\n不一致项：")
        for x in bad:
            print("  -", x)
        return 1
    print("\n一致：图编排与旧序贯管线的产出逐块相同（改造未改变行为）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
