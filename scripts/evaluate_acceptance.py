"""用本地模型评估一轮真实事件流，不参与游戏状态。"""
from __future__ import annotations

import argparse, asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.config import load_config
from server.llm import build_provider


PROMPT = """你是跑团验收评估 Agent。根据每条玩家行为和完整事件流，先判断确定性事实，再评价语言质量。
确定性事实优先看：dice 的 success、system 的移动文本、state 的 current_scene/flags，以及是否出现 agent_note error。
判断：1) 行为理解是否合理；2) 是否错误限制自由度；3) 是否错误推进剧情；4) 是否产生未确认事实；5) 旁白是否泄露内幕或重复。
只输出 JSON 数组，每项：case_id、verdict（pass/fail/review）、deterministic（对象，字段 state_ok/move_ok/facts_ok）、issues（数组）、reason（不超过120字）。
不要因为玩家行动没有对应检定就判错；没有检定也可以是合理普通行动。不要把“没有使用固定拒绝词”当成错误。
"""

async def _evaluate_batch(provider, rows, max_chars: int):
    """按字符预算评估；超限时递归二分，适配不同本地模型上下文。"""
    payload = json.dumps(rows, ensure_ascii=False)
    if len(payload) > max_chars and len(rows) > 1:
        mid = max(1, len(rows) // 2)
        left = await _evaluate_batch(provider, rows[:mid], max_chars)
        right = await _evaluate_batch(provider, rows[mid:], max_chars)
        return left + right
    try:
        raw = await provider.chat([
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": payload},
        ])
    except Exception:
        # 单条仍可能因极长事件流超限；让调用方保留明确错误，而不是静默丢样本。
        if len(rows) > 1:
            mid = max(1, len(rows) // 2)
            left = await _evaluate_batch(provider, rows[:mid], max_chars)
            right = await _evaluate_batch(provider, rows[mid:], max_chars)
            return left + right
        raise
    text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
    try:
        batch = json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("["), text.rfind("]")
        batch = json.loads(text[a:b + 1]) if a >= 0 and b > a else []
    return batch if isinstance(batch, list) else []


async def main(path: str, out: str, max_chars: int = 10000) -> int:
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
            report.extend(await _evaluate_batch(provider, rows[start:start + 4], max_chars))
    finally:
        await provider.close()
    if not isinstance(report, list):
        print("评估 Agent 没有返回数组", file=sys.stderr); return 1
    # 模型输出只是语义建议，case 集合由服务端输入决定。过滤模型臆造的事件 id、
    # 去掉重复项，并把漏评用例标成 review，不能让评估器靠少报样本制造高分。
    expected = [str(row.get("case", {}).get("id") or "") for row in rows]
    expected_set = set(expected)
    normalized, seen = [], set()
    for item in report:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or "")
        if case_id not in expected_set or case_id in seen:
            continue
        seen.add(case_id)
        normalized.append(item)
    for case_id in expected:
        if case_id not in seen:
            normalized.append({
                "case_id": case_id, "verdict": "review",
                "deterministic": {"state_ok": None, "move_ok": None, "facts_ok": None},
                "issues": ["评估 Agent 未返回该用例"],
                "reason": "需要人工复核",
            })
    report = normalized
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
    ap.add_argument("--max-chars", type=int, default=10000,
                    help="每批评估输入的字符预算，超限自动二分（默认 10000）")
    args = ap.parse_args(); raise SystemExit(asyncio.run(main(args.results, args.out, args.max_chars)))
