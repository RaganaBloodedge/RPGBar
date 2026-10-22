# -*- coding: utf-8 -*-
"""A/B 计时探针：建房 → 开局 → 3 次行动 → 2 次建议，逐段计时。

用服务端的 `thinking` 事件做边界（active=True → False 才是一次完整产出），
不拿 narration 当完成信号 —— 一次行动可能连发多条 narration，容易把下一条
测成 0.0 s。用法：起服务器后 `python scripts/_bench_round.py 标签`。
"""
import asyncio, json, subprocess, sys, time
import websockets

URI = "ws://127.0.0.1:8021/ws"
TAG = sys.argv[1] if len(sys.argv) > 1 else "run"
SCRIPT = "scripts/totsk_l1.json"


def gpu_mem():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()[0]
        used, total = [x.strip() for x in out.split(",")]
        return f"{used}/{total} MiB"
    except Exception:
        return "?"


async def main():
    rows = []
    trace = []
    async with websockets.connect(URI, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "join", "name": "李维",
            "character": {"name": "李维", "profession": "探险家"},
            "create": {"script": SCRIPT, "max_players": 2, "visibility": "public"},
        }, ensure_ascii=False))

        async def until_done(who, timeout=900):
            """等到 who 的 thinking 从 True 走回 False（或先见到产出的 type）。"""
            started, end = False, time.time() + timeout
            while time.time() < end:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                except asyncio.TimeoutError:
                    continue
                t = msg.get("type")
                trace.append(t)
                if t == "thinking" and msg.get("who") == who:
                    started = started or bool(msg.get("active"))
                    if started and not msg.get("active"):
                        return True
                elif t in ("narration", "suggestions") and started:
                    continue
            return False

        # 等 welcome（在此之前会有 system 广播）
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if msg.get("type") == "welcome":
                break

        # 开局旁白是剧本原文（不经过模型），所以直接从第一次行动开始计时
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(1.5)

        for i, act in enumerate(("我仔细打量门楣上的浮雕", "我跟卡特搭话，问他这墓的来历",
                                 "我蹲下看石门前的地面"), 1):
            t = time.time()
            await ws.send(json.dumps({"type": "action", "text": act}, ensure_ascii=False))
            await until_done("dm")
            rows.append((f"行动 {i}：{act[:10]}", time.time() - t))

        for i in (1, 2):
            t = time.time()
            await ws.send(json.dumps({"type": "suggest"}))
            await until_done("advisor")
            rows.append((f"建议 {i}", time.time() - t))

    print(f"\n=== [{TAG}] 显存 {gpu_mem()} ===")
    for name, sec in rows:
        print(f"  {name:<26} {sec:6.1f} s")
    warm = [s for n, s in rows if n.startswith("行动")]
    print(f"  行动合计（3 次）  {sum(warm):6.1f} s")
    print(f"  消息流水：{' '.join(trace[:60])}")


asyncio.run(main())
