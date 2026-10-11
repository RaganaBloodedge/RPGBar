#!/usr/bin/env python3
"""engine_probe.py —— 本机内置引擎自检。

回答一个问题：**这台机器现在能不能用内置引擎**（rpg_engine.dll + Qwen3 权重）。
不需要启动服务器，也不写任何文件。

    python scripts/engine_probe.py
    python scripts/engine_probe.py --lib <路径> --model <路径>
    python scripts/engine_probe.py --say "用一句话介绍你自己。" --no-think

退出码：0 = 可用；2 = 库或权重缺失（游戏会自动退回脚本化兜底，**不是故障**）。
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import engine as engine_mod  # noqa: E402
from server.llm import build_provider  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default="", help="rpg_engine.dll 路径（默认自动探测）")
    ap.add_argument("--model", default="", help="Qwen3 GGUF 路径（默认自动探测）")
    ap.add_argument("--context", type=int, default=1024, help="会话上下文容量")
    ap.add_argument("--max-tokens", type=int, default=48)
    ap.add_argument("--say", default="用一句话介绍你自己。")
    ap.add_argument("--think", dest="think", action="store_true", help="开启思考模式")
    ap.add_argument("--no-think", dest="think", action="store_false")
    ap.set_defaults(think=False)
    args = ap.parse_args()

    lib = engine_mod.find_lib(args.lib)
    weights = engine_mod.find_model(args.model)
    print("引擎库 :", lib or "（没找到）")
    print("权重   :", weights or "（没找到）")
    if lib is None or weights is None:
        print("\n内置引擎不可用 —— 游戏会退回脚本化兜底，这不是故障。")
        print("把 rpg_engine.dll 与 Qwen3-*.gguf 放进 models/，或用 --lib / --model 指定。")
        return 2

    provider = build_provider({
        "kind": "engine", "lib": str(lib), "model": str(weights),
        "context": args.context, "max_tokens": args.max_tokens, "think": args.think,
    })
    if provider is None:
        print("\n构建 Provider 失败（库与权重都在，多半是库版本不匹配）。")
        return 1

    lib_obj = engine_mod.open_lib(lib)
    model = lib_obj.model_for(weights, threads=0, max_pos=args.context)
    info = lib_obj.model_info(model)
    print("引擎版本:", lib_obj.version())
    print("模型    :", f"{info.get('name') or info.get('arch')} · {info.get('n_layers')} 层"
                      f" · hidden {info.get('hidden')} · 词表 {info.get('vocab')}"
                      f" · 权重 {info.get('weight_bytes', 0) / 2**30:.2f} GiB")
    print("上下文  :", f"{args.context}（思考模式：{'开' if args.think else '关'}）")

    async def run() -> None:
        print("\n---- 单轮对话 ----")
        print("提问：", args.say)
        t0 = time.time()
        text = await provider.chat([{"role": "user", "content": args.say}])
        dt = time.time() - t0
        usage = provider.last_usage or {}
        n = usage.get("completion_tokens") or 0
        print("回答：", text)
        print(f"耗时 {dt:.1f}s / 生成 {n} token"
              + (f"（{n / dt:.1f} tok/s）" if dt > 0 and n else ""))

        print("\n---- 工具调用 ----")
        tools = [{
            "type": "function",
            "function": {
                "name": "read_scene",
                "description": "读取当前场景的文本",
                "parameters": {
                    "type": "object",
                    "properties": {"scene": {"type": "string", "description": "场景名"}},
                    "required": ["scene"],
                },
            },
        }]
        t0 = time.time()
        reply = await provider.chat_with_tools(
            [{"role": "user", "content": "查看一下当前场景。"}], tools
        )
        print("耗时 %.1fs" % (time.time() - t0))
        print("content   :", (reply.get("content") or "")[:200] or "（空）")
        print("tool_calls:", json.dumps(reply.get("tool_calls") or [], ensure_ascii=False))
        await provider.close()

    asyncio.run(run())
    print("\n自检通过：内置引擎可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
