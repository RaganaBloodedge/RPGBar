"""通过真实 WebSocket 验证不同玩法入口的公开契约和规则绑定。

这个检查不依赖剧本文件名来判断行为，只检查服务端为房间发布的
PlayDefinition：玩法模式、规则引用、知识引用和生成策略必须来自所选内容。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import PurePosixPath


CASES = (
    {
        "name": "固定冒险",
        "script": "scripts/sample_script.json",
        "mode": "scenario",
        "rules_ref": "generic_d20",
        "generation_policy": "prebuilt_scene_graph",
    },
    {
        "name": "CoC 沙盒",
        "script": "scripts/coc_sandbox.json",
        "mode": "sandbox",
        "rules_ref": "d100",
        "generation_policy": "session_zero",
    },
)


async def receive_welcome(ws):
    for _ in range(20):
        msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if msg.get("type") == "welcome":
            return msg
    raise AssertionError("未收到 welcome")


async def run(url: str):
    import websockets

    results = []
    for case in CASES:
        async with websockets.connect(url, max_size=8 * 1024 * 1024,
                                      ping_interval=10) as ws:
            await ws.send(json.dumps({
                "type": "join",
                "name": f"玩法验收-{case['name']}",
                "create": {
                    "script": case["script"],
                    "max_players": 1,
                    "visibility": "private",
                },
            }, ensure_ascii=False))
            welcome = await receive_welcome(ws)
            info = welcome.get("script_info") or {}
            observed = {
                "script": info.get("path"),
                "mode": info.get("mode"),
                "rules_ref": info.get("rules_ref"),
                "generation_policy": info.get("generation_policy"),
                "knowledge_ref": info.get("knowledge_ref"),
            }
            for key in ("mode", "rules_ref", "generation_policy"):
                assert observed[key] == case[key], (
                    case["name"], key, observed, case
                )
            # 公开契约不会泄露服务器目录，只返回 scripts/ 下的相对文件名。
            assert observed["script"] == PurePosixPath(case["script"]).name, observed
            results.append({"name": case["name"], "observed": observed})
    print(json.dumps({"cross_play_live": "PASS", "cases": results},
                     ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8010/ws")
    args = ap.parse_args()
    asyncio.run(run(args.url))


if __name__ == "__main__":
    main()
