"""实跑一局：连本机服务器 → 建房 → 开局 → 发几条行动 → 打印 DM 旁白。

用法：
    python scripts/play_kb.py --url ws://127.0.0.1:8765/ws --room KB1 \
        --script totsk_l1.json --actions scripts/play_kb_actions.txt

它只做三件事：发消息、收事件、把 `narration` 与（如果有）`function` 事件打出来。
DM 用的是不是知识库工具，看服务端日志（kb_lookup / kb_lore 会出现在工具轨迹里）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

DEFAULT_ACTIONS = [
    "我先打量一下四周，看看这间墓室有什么值得注意的东西。",
    "那个从水里爬出来的东西是什么？它有多强，我要怎么对付它？",
    "我掏出手枪，检查一下它的射程和伤害。",
    "我要用说服检定让守墓人放我过去，规则上该怎么判？",
]


async def recv_until(ws, pred, timeout=900.0):
    end = asyncio.get_event_loop().time() + timeout
    last = None
    while True:
        left = end - asyncio.get_event_loop().time()
        if left <= 0:
            return last
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
        except asyncio.TimeoutError:
            return last
        if pred(msg):
            return msg
        last = msg


async def wait_turn(ws, timeout=900.0):
    """收一轮：等到 narration（DM 旁白）之后 thinking 归位，或直接超时。"""
    got = []
    narr = None
    end = asyncio.get_event_loop().time() + timeout
    while True:
        left = end - asyncio.get_event_loop().time()
        if left <= 0:
            break
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=min(left, 30)))
        except asyncio.TimeoutError:
            if narr:
                break
            continue
        t = msg.get("type")
        if t == "narration":
            narr = msg
            print(f"\n[DM] {msg.get('text')}")
        elif t == "dice":
            print("[骰] " + json.dumps(
                {k: msg.get(k) for k in ("skill", "dice", "roll", "target", "level",
                                         "success", "skill_value", "skill_source", "attr")},
                ensure_ascii=False))
        elif t == "system":
            print(f"[系统] {msg.get('text')}")
        elif t == "agent_note":
            print("[#] " + json.dumps(msg, ensure_ascii=False)[:400])
        elif t == "thinking" and msg.get("who") == "dm" and not msg.get("active"):
            if narr:
                break
        elif t == "error":
            print(f"[错误] {msg.get('message')}")
        got.append(t)
    return narr


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    ap.add_argument("--room", default="KB1")
    ap.add_argument("--script", default="totsk_l1.json")
    ap.add_argument("--name", default="测试调查员")
    ap.add_argument("--actions", default="")
    args = ap.parse_args()

    try:
        import websockets
    except ImportError:
        print("缺 websockets：pip install websockets")
        return 2

    actions = DEFAULT_ACTIONS
    if args.actions:
        with open(args.actions, encoding="utf-8") as f:
            actions = [ln.strip() for ln in f if ln.strip()]

    async with websockets.connect(args.url, proxy=None, max_size=8 << 20) as ws:
        await ws.send(json.dumps({
            "type": "join", "name": args.name, "room": args.room,
            "script": args.script, "character": {"cls": "记者"},
        }, ensure_ascii=False))
        snap = await recv_until(ws, lambda m: m.get("type") == "room", timeout=60)
        if not snap:
            print("没能进房")
            return 1
        room = snap.get("room") or {}
        print(f"[房间] {room.get('code')} phase={room.get('phase')} 剧本={room.get('script')}")
        if room.get("phase") != "playing":
            await ws.send(json.dumps({"type": "start"}, ensure_ascii=False))
            snap = await recv_until(
                ws, lambda m: m.get("type") == "room" and (m.get("room") or {}).get("phase") == "playing",
                timeout=120)
            print(f"[开局] {'ok' if snap else '失败'}")
            await wait_turn(ws, timeout=300)  # 开局旁白

        for i, act in enumerate(actions, 1):
            print(f"\n=== 第 {i} 轮：{act}")
            await ws.send(json.dumps({"type": "action", "text": act}, ensure_ascii=False))
            narr = await wait_turn(ws, timeout=900)
            if not narr:
                print("[!] 这一轮没等到旁白")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
