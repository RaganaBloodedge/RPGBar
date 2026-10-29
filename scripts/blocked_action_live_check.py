"""真实本地模型验收：无机制行动不能被叙述成已完成。"""
from __future__ import annotations

import argparse
import asyncio
import json
import re


async def run(url: str):
    import websockets

    async with websockets.connect(url, max_size=8 * 1024 * 1024,
                                  ping_interval=10) as ws:
        await ws.send(json.dumps({
            "type": "join", "name": "不可承接动作验收",
            "create": {"script": "scripts/sample_script.json", "max_players": 1,
                        "visibility": "private"},
        }, ensure_ascii=False))
        started = False
        sent = False
        events = []
        for _ in range(80):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 90))
            events.append(msg)
            if msg.get("type") == "welcome" and not started:
                await ws.send(json.dumps({"type": "start"}))
                started = True
            elif started and not sent and msg.get("type") == "state":
                await ws.send(json.dumps({"type": "action", "text": "我生火取暖"},
                                         ensure_ascii=False))
                sent = True
            elif sent and msg.get("type") == "thinking" \
                    and msg.get("who") == "dm" and msg.get("active") is False:
                break
        facts = [m.get("event") for m in events if m.get("type") == "fact_event"]
        narrations = [m.get("text", "") for m in events if m.get("type") == "narration"]
        text = "\n".join(narrations)
        assert sent, "未发送行动"
        assert not any(f and f.get("type") in {"check_resolved", "scene_entered"}
                       for f in facts), facts
        # 允许模型使用自然语言拒绝，但不能宣称火已点燃/行动成功。
        assert not re.search(r"燃起来|点燃成功|火堆成功|成功生火", text), text
        assert re.search(r"不能|无法|不行|没有|做不到|不允许|未能|没法", text), text
        print(json.dumps({"blocked_action_live": "PASS", "narration": text[:500]},
                         ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8010/ws")
    asyncio.run(run(ap.parse_args().url))


if __name__ == "__main__":
    main()
