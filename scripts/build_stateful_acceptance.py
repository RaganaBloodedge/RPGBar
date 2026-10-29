"""从切分剧本结构生成带状态前置条件的验收集骨架。

它只生成机制边界和前置证据；自然语言可以再由模型扩写，但不能删除
scene/flags/checks/exits 这些服务端事实条件。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.rag import ScriptStore


def pre(scene, *, flags=None, checks=None, exits=None):
    return {"scene": scene, "flags": list(flags or []),
            "checks": list(checks or []), "exits": list(exits or [])}


def build(script: str, limit: int = 25) -> dict:
    store = ScriptStore(Path(script))
    rows = []
    for scene in store.scenes.values():
        sid = scene["id"]
        rows.append({"text": "我现在在哪儿？", "category": "fact_question",
                     "expect": ["answer_location", "no_tool"],
                     "precondition": pre(sid)})
        rows.append({"text": f"我观察{scene.get('title') or '这里'}周围的细节。",
                     "category": "normal_action", "expect": ["no_move"],
                     "precondition": pre(sid)})
        for check in scene.get("checks", []) or []:
            kws = [str(k) for k in (check.get("keywords") or []) if str(k).strip()]
            trigger = kws[0] if kws else str(check.get("skill") or "这个动作")
            rows.append({"text": f"我仔细{trigger}，确认现场有什么变化。",
                         "category": "check_action", "expect": ["roll"],
                         "precondition": pre(sid, checks=[check.get("id")])})
        for exit_ in scene.get("exits", []) or []:
            cond = exit_.get("condition")
            rows.append({"text": f"我{exit_.get('label') or ('前往' + str(exit_.get('to')))}。",
                         "category": "move", "expect": ["move"],
                         "precondition": pre(sid, flags=[cond] if cond else [],
                                              exits=[exit_.get("to")]),
                         "expected_state": {"scene_after": exit_.get("to")}})
        rows.append({"text": "我宣布这件事已经完成，直接跳过剩下的过程。",
                     "category": "blocked_action", "expect": ["no_move", "no_secret"],
                     "precondition": pre(sid)})
    for i, row in enumerate(rows[:limit], 1):
        row["id"] = f"stateful_{i:03d}"
    return {"script": script, "title": store.title, "generated_at": time.time(),
            "generator": "structure", "cases": rows[:limit]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()
    data = build(args.script, max(1, args.limit))
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成 {len(data['cases'])} 条结构化状态用例 → {args.out}")


if __name__ == "__main__":
    main()
