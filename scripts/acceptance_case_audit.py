"""审计剧本驱动验收集的前置条件，不执行模型。

验收失败必须区分两件事：DM 没理解行动，还是用例本身在当前状态不可执行。
本工具只使用 ScriptStore 的场景、出口、检定和 NPC 结构，不维护剧本词表。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.rag import ScriptStore


def terms_for_exit(store, scene, exit_):
    target = store.get_scene(str(exit_.get("to") or "")) or {}
    return [str(x) for x in (
        *(exit_.get("keywords") or []), exit_.get("label"), target.get("title"),
        target.get("location"), *(target.get("keywords") or []),
    ) if x]


def audit(cases_path: str) -> dict:
    data = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    store = ScriptStore(Path(data["script"]))
    scenes = list(store.scenes.values())
    rows = []
    for case in data.get("cases", []):
        text = str(case.get("text") or "")
        expect = set(case.get("expect") or [])
        pre = case.get("precondition") or {}
        findings = []
        if case.get("category") == "conditional_move" and not pre:
            findings.append({"kind": "missing_precondition", "severity": "error",
                             "message": "conditional_move 必须声明 scene/flags 前置条件"})
        if "move" in expect:
            if not pre.get("scene"):
                findings.append({"kind": "missing_scene", "severity": "warning",
                                 "message": "期望移动但没有声明起始场景"})
            destinations = [
                (s, e) for s in scenes for e in (s.get("exits") or [])
                if any(term in text for term in terms_for_exit(store, s, e))
            ]
            if not destinations:
                findings.append({"kind": "unknown_destination", "severity": "error",
                                 "message": "文本没有命中任何剧本出口/目标地点证据"})
        if "roll" in expect:
            checks = [c for s in scenes for c in (s.get("checks") or [])]
            if not any(any(str(k) in text for k in (c.get("keywords") or [])) for c in checks):
                findings.append({"kind": "unbound_check", "severity": "warning",
                                 "message": "文本没有命中切分器提供的检定触发词，需人工确认自然语言语义是否足够"})
        names = {
            str(n.get("name")) for s in scenes for n in (s.get("npcs") or [])
            if isinstance(n, dict) and n.get("name")
        }
        mentioned = [name for name in names if name in text]
        if mentioned and not pre.get("scene"):
            possible = [s.get("id") for s in scenes if any(
                isinstance(n, dict) and n.get("name") in mentioned for n in (s.get("npcs") or []))]
            if len(set(possible)) > 1 or not possible:
                findings.append({"kind": "npc_scope_ambiguous", "severity": "warning",
                                 "message": "提到了 NPC 但没有声明当前场景，无法验证是否在场",
                                 "npcs": mentioned})
        if findings:
            rows.append({"id": case.get("id"), "text": text,
                         "category": case.get("category"), "findings": findings})
    return {"cases": len(data.get("cases", [])), "findings": rows,
            "errors": sum(1 for r in rows for f in r["findings"] if f["severity"] == "error"),
            "warnings": sum(1 for r in rows for f in r["findings"] if f["severity"] == "warning")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    ap.add_argument("--out")
    args = ap.parse_args()
    report = audit(args.cases)
    out = Path(args.out) if args.out else Path(args.cases).with_name(Path(args.cases).stem + "_audit.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("cases", "errors", "warnings")}, ensure_ascii=False))
    print(f"审计报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
