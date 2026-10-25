"""剧本驱动的玩家行为验收集生成器与执行器。

它不维护一份绑定某个模组的关键词表，而是把当前剧本的场景、出口、检定、
在场物件和硬约束交给本地 DM 模型，请模型生成“玩家会怎么说”以及每句话的
可观察验收条件。换剧本后重新生成即可得到不同的测试集。

用法：
  py -3.14 scripts/player_acceptance.py generate --script sample_script.json
  py -3.14 scripts/player_acceptance.py run --cases data/acceptance_cases.json --url ws://127.0.0.1:8000/ws
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import load_config
from server.llm import build_provider
from server.rag import ScriptStore


GEN_PROMPT = """你是跑团游戏的验收设计 Agent。根据下面这份剧本，生成一组真实玩家会说的话。
不要复述剧本，不要生成小说。输出 JSON 数组，每项字段：
text（玩家原话）、category（fact_question / normal_action / check_action / move /
blocked_action / meta_question / ambiguous）、expect（数组，允许：no_tool、roll、move、
no_move、state_change、refuse、answer_location、no_secret、no_duplicate）、
reason（为什么应该这样判定）。

要求（必须生成至少 24 条，少于 24 条视为失败）：
前 7 条必须按顺序各使用一次 category：fact_question、normal_action、check_action、
move、blocked_action、meta_question、ambiguous；之后再扩展覆盖场景和出口。
1. 每个场景至少一条；每个出口至少一条合法移动和一条未满足条件的尝试（如果有门控）。
2. 每个检定至少一条自然口语触发句，不能直接抄 keywords。
3. 至少四条剧本外动作，动作必须从当前场景的物件和规则推导，不能使用固定词表。
4. 至少三条“我在哪儿/周围有什么/刚才发生了什么”事实问题。
5. 至少两条元问题，至少两条模糊指代，至少两条会诱导 DM 泄露内幕或重复旁白的话。
6. 玩家语句要像人说话，不能出现“测试、断言、期望、关键词”等测试术语。
7. 只输出 JSON 数组。

【剧本】
{script}
"""


def _script_context(store: ScriptStore) -> str:
    chunks = []
    for sid, scene in store.scenes.items():
        chunks.append(json.dumps({
            "id": sid, "title": scene.get("title"), "location": scene.get("location"),
            "public_text": scene.get("public_text"), "dm_notes": scene.get("dm_notes"),
            "npcs": scene.get("npcs", []), "keywords": scene.get("keywords", []),
            "checks": scene.get("checks", []), "exits": scene.get("exits", []),
        }, ensure_ascii=False))
    return "\n".join(chunks)


def _json(text: str):
    text = (text or "").strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("["), text.rfind("]")
        return json.loads(text[a:b + 1]) if a >= 0 and b > a else None


async def generate(script: str, out: str) -> int:
    store = ScriptStore(Path(script), label=Path(script).name)
    cfg = load_config()
    slot = dict(cfg.get("models", {}).get("dm", {}))
    # 验收集需要比一轮旁白更多的 JSON；只提高本次生成上限，不改变游戏配置。
    slot["max_tokens"] = max(2048, int(slot.get("max_tokens") or 0))
    provider = build_provider(slot)
    if provider is None:
        print("DM 模型不可用，无法生成剧本驱动验收集", file=sys.stderr)
        return 2
    try:
        raw = await provider.chat([
            {"role": "system", "content": "你负责设计可执行的玩家行为验收集。"},
            {"role": "user", "content": GEN_PROMPT.format(script=_script_context(store))},
        ])
    finally:
        await provider.close()
    try:
        cases = _json(raw)
    except json.JSONDecodeError:
        cases = None
    if not isinstance(cases, list):
        print("模型没有返回 JSON 数组", file=sys.stderr)
        return 1
    clean = []
    for c in cases:
        if not isinstance(c, dict) or not str(c.get("text", "")).strip():
            continue
        expect = c.get("expect") if isinstance(c.get("expect"), list) else []
        clean.append({"id": f"case_{len(clean)+1:03d}", "text": str(c["text"]).strip(),
                      "category": str(c.get("category") or "ambiguous"),
                      "expect": [str(x) for x in expect],
                      "reason": str(c.get("reason") or "")[:200]})
    counts = {}
    for c in clean:
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    required = {"fact_question", "normal_action", "check_action", "move",
                "blocked_action", "meta_question", "ambiguous"}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"script": str(script), "title": store.title,
                                     "generated_at": time.time(), "coverage": counts,
                                     "cases": clean},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成 {len(clean)} 条剧本驱动验收用例 → {out}")
    missing = sorted(required - set(counts))
    if missing:
        print("覆盖不足，缺少类别：" + "、".join(missing), file=sys.stderr)
        return 3
    return 0


async def run_cases(cases_path: str, url: str) -> int:
    import websockets
    data = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    results = []
    async with websockets.connect(url, max_size=8 * 1024 * 1024, ping_interval=10) as ws:
        await ws.send(json.dumps({"type": "join", "name": "验收玩家",
                                  "create": {"script": data["script"], "max_players": 1,
                                              "visibility": "private"}}, ensure_ascii=False))
        started = False
        phase = "joining"
        pending = None
        observed = []
        while cases or pending is not None:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 45))
            except asyncio.TimeoutError:
                tail = [m.get("type") for m in observed[-8:]]
                raise RuntimeError(f"阶段 {phase} 等待事件超过 45 秒；最近事件={tail}")
            typ = msg.get("type")
            if pending is not None:
                observed.append(msg)
            if typ == "welcome" and not started:
                started = True
                phase = "starting"
                await ws.send(json.dumps({"type": "start"}))
            if typ == "room" and (msg.get("room") or {}).get("phase") == "playing" and pending is None:
                pending = cases.pop(0)
                observed = []
                phase = f"action:{pending.get('id', pending.get('text', '')[:12])}"
                await ws.send(json.dumps({"type": "action", "text": pending["text"]}, ensure_ascii=False))
            if typ == "thinking" and msg.get("who") == "dm" and msg.get("active") is False and pending:
                types = {m.get("type") for m in observed}
                text = " ".join(str(m.get("text") or m.get("message") or "") for m in observed)
                checks = {
                    "roll": "dice" in types,
                    "move": any(m.get("type") == "system" and "进入" in str(m.get("text")) for m in observed),
                    "no_move": not any(m.get("type") == "system" and "进入" in str(m.get("text")) for m in observed),
                    "state_change": "state" in types,
                    "refuse": any(x in text for x in ("没有", "不能", "做不到", "不可能", "无法")),
                    "answer_location": any(x in text for x in ("在", "地点", "场景")),
                    "no_duplicate": len([m for m in observed if m.get("type") == "narration"]) <= 3,
                    "no_tool": "dice" not in types and not any(m.get("type") == "system" and "进入" in str(m.get("text")) for m in observed),
                    "no_secret": not any(x in text for x in ("DM内幕", "内幕", "绝对不要")),
                }
                expected = pending.get("expect") or []
                passed = all(checks.get(x, False) for x in expected)
                results.append({"case": pending, "observed_types": sorted(types),
                                "checks": checks, "passed": passed, "text": text[:1200],
                                "events": observed[:]})
                pending = None
                phase = "waiting_next_action"
        Path(cases_path).with_name(Path(cases_path).stem + "_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    # 输出与剧本无关的边界指标，便于比较不同 Agent/提示词版本。
    totals = {key: sum(1 for r in results if r.get("checks", {}).get(key))
              for key in ("roll", "move", "no_move", "refuse", "no_tool",
                          "answer_location", "no_secret", "no_duplicate")}
    by_category = {}
    for row in results:
        category = row.get("case", {}).get("category", "ambiguous")
        stat = by_category.setdefault(category, {"total": 0, "passed": 0})
        stat["total"] += 1
        if row.get("passed"):
            stat["passed"] += 1
    passed = sum(1 for r in results if r.get("passed"))
    report = {"cases": len(results), "passed": passed, "pass_rate": round(passed / len(results), 4) if results else 0,
              "observed": totals, "by_category": by_category,
              "failures": [r["case"] for r in results if not r.get("passed")]}
    report_path = Path(cases_path).with_name(Path(cases_path).stem + "_report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"执行 {len(results)} 条用例；通过 {passed} 条（{report['pass_rate']:.0%}）")
    print("边界观测：" + "，".join(f"{k}={v}" for k, v in totals.items()))
    print(f"逐条结果：{Path(cases_path).with_name(Path(cases_path).stem + '_results.json')}")
    print(f"汇总报告：{report_path}")
    return 0 if passed == len(results) else 4


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate"); g.add_argument("--script", required=True); g.add_argument("--out", default="data/acceptance_cases.json")
    r = sub.add_parser("run"); r.add_argument("--cases", required=True); r.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    args = ap.parse_args()
    if args.cmd == "generate": return asyncio.run(generate(args.script, args.out))
    return asyncio.run(run_cases(args.cases, args.url))


if __name__ == "__main__":
    raise SystemExit(main())
