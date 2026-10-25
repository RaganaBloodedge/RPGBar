"""用本地模型评估一轮真实事件流，不参与游戏状态。"""
from __future__ import annotations

import argparse, asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.config import load_config
from server.llm import build_provider


PROMPT = """你是跑团验收评估 Agent。根据每条玩家行为和完整事件流，判断：
1) 行为理解是否合理；2) 是否错误限制自由度；3) 是否错误推进剧情；4) 是否产生未确认事实；5) 旁白是否泄露内幕或重复。
只输出 JSON 数组，每项：case_id、verdict（pass/fail/review）、issues（数组）、reason（不超过120字）。
不要因为玩家行动没有对应检定就判错；先看事件中服务端是否实际改变状态。
"""

async def main(path: str, out: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("results", [])
    slot = dict(load_config().get("models", {}).get("dm", {}))
    slot["max_tokens"] = max(1024, int(slot.get("max_tokens") or 0))
    provider = build_provider(slot)
    if provider is None:
        print("DM 模型不可用", file=sys.stderr); return 2
    report = []
    try:
        for start in range(0, len(rows), 4):
            raw = await provider.chat([
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": json.dumps(rows[start:start + 4], ensure_ascii=False)},
            ])
            text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
            try:
                batch = json.loads(text)
            except json.JSONDecodeError:
                a, b = text.find("["), text.rfind("]")
                batch = json.loads(text[a:b + 1]) if a >= 0 and b > a else []
            if isinstance(batch, list):
                report.extend(batch)
    finally:
        await provider.close()
    if not isinstance(report, list):
        print("评估 Agent 没有返回数组", file=sys.stderr); return 1
    Path(out).write_text(json.dumps({"source": path, "evaluations": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {}
    for row in report:
        v = str(row.get("verdict") or "review")
        counts[v] = counts.get(v, 0) + 1
    print(f"评估 {len(report)} 条：" + "，".join(f"{k}={v}" for k, v in counts.items()))
    print(f"报告：{out}")
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--results", required=True); ap.add_argument("--out", required=True)
    args = ap.parse_args(); raise SystemExit(asyncio.run(main(args.results, args.out)))
