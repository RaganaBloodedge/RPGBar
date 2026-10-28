"""连接真实服务验证 CoC 沙盒开局事件。用法：py ... --url ws://127.0.0.1:8010/ws"""
from __future__ import annotations

import argparse
import asyncio
import json


async def run(url: str):
    import websockets

    async with websockets.connect(url, max_size=8 * 1024 * 1024, ping_interval=10) as ws:
        await ws.send(json.dumps({
            "type": "join", "name": "SessionZero验收",
            "create": {"script": "scripts/coc_sandbox.json", "max_players": 1,
                        "visibility": "private"},
        }, ensure_ascii=False))
        started = False
        events = []
        for _ in range(40):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 45))
            events.append(msg)
            if msg.get("type") == "welcome" and not started:
                await ws.send(json.dumps({"type": "start"}))
                started = True
            if msg.get("type") == "thinking" and msg.get("who") == "dm" \
                    and msg.get("active") is False and started:
                break
        facts = [m.get("event") for m in events if m.get("type") == "fact_event"]
        assert any(f and f.get("type") == "world_changed"
                   and f.get("target") == "session_zero" for f in facts), facts
        print("session zero live: PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8010/ws")
    args = ap.parse_args()
    asyncio.run(run(args.url))


if __name__ == "__main__":
    main()
