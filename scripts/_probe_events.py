# -*- coding: utf-8 -*-
"""观察：开局后 90 秒内的原始消息流水（type / who / active + 相对时间）。"""
import asyncio, json, sys, time
import websockets

URI = "ws://127.0.0.1:8021/ws"


async def main():
    t0 = time.time()
    async with websockets.connect(URI, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "join", "name": "李维",
            "character": {"name": "李维", "profession": "探险家"},
            "create": {"script": "scripts/totsk_l1.json", "max_players": 2, "visibility": "public"},
        }, ensure_ascii=False))
        started = False
        end = t0 + float(sys.argv[1] if len(sys.argv) > 1 else 120)
        while time.time() < end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
            except asyncio.TimeoutError:
                continue
            t = msg.get("type")
            extra = ""
            if t == "thinking":
                extra = f"{msg.get('who')} active={msg.get('active')} {msg.get('label','')}"
            elif t == "narration":
                extra = (msg.get("text") or "")[:40].replace("\n", " ")
            elif t == "system":
                extra = (msg.get("text") or "")[:40]
            elif t == "suggestions":
                extra = str(msg.get("options"))[:60]
            print(f"{time.time()-t0:7.1f}s  {t:<12} {extra}", flush=True)
            if t == "welcome" and not started:
                started = True
                await ws.send(json.dumps({"type": "start"}))
                print(f"{time.time()-t0:7.1f}s  >>> start", flush=True)
                asyncio.get_event_loop().call_later(
                    3.0, lambda: asyncio.ensure_future(
                        ws.send(json.dumps({"type": "action", "text": "我仔细打量门楣上的浮雕"},
                                           ensure_ascii=False))))

asyncio.run(main())
