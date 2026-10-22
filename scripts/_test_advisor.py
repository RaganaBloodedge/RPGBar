# -*- coding: utf-8 -*-
"""实测 advisor 建议：建房 → 进场景 → 做动作 → 请求建议。"""
import asyncio, json, sys
import websockets

URI = "ws://127.0.0.1:8021/ws"

async def main():
    async with websockets.connect(URI, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "join", "name": "李维",
            "character": {"name": "李维", "profession": "探险家"},
            "create": {"script": "scripts/totsk_l1.json", "max_players": 2, "visibility": "public"},
        }, ensure_ascii=False))
        # 等 joined
        joined = None
        for _ in range(50):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if msg.get("type") == "welcome":
                joined = msg
                break
        if not joined:
            print("未收到 welcome", flush=True); sys.exit(1)
        print("== 房间:", joined.get("room"), flush=True)
        # 房主开局
        await ws.send(json.dumps({"type": "start"}))
        for _ in range(60):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if msg.get("type") == "narration":
                print("== 开场旁白已出", flush=True)
                break

        async def drain_until(want, timeout=120):
            end = asyncio.get_event_loop().time() + timeout
            out = None
            while asyncio.get_event_loop().time() < end:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                except asyncio.TimeoutError:
                    continue
                t = msg.get("type")
                if t == want:
                    out = msg
                    break
            return out

        async def suggest(tag):
            await ws.send(json.dumps({"type": "suggest"}))
            m = await drain_until("suggestions", 120)
            ag = m.get("agent", {}) or {}
            print(f"\n== 建议[{tag}] source={ag.get('source')}"
                  f" stopped={ag.get('stopped')} err={ag.get('error') or '-'}", flush=True)
            for o in m.get("options", []):
                print("  -", o, flush=True)

        async def act(text):
            await ws.send(json.dumps({"type": "action", "text": text}, ensure_ascii=False))
            await drain_until("narration", 120)

        await suggest("开场（未做任何动作）")
        await act("我仔细打量门楣上的浮雕")
        await suggest("打量浮雕后")
        await act("我跟卡特搭话，问他这墓的来历")
        await suggest("与卡特搭话后")
        await act("我蹲下看石门前的地面")
        await suggest("看地面后")

asyncio.run(main())
