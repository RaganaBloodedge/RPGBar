#!/usr/bin/env python3
"""engine_probe.py —— 本机内置引擎自检。

回答一个问题：**这台机器现在能不能用内置引擎**（rpg_engine.dll + Qwen3 权重）。
不需要启动服务器，也不写任何文件。

    python scripts/engine_probe.py
    python scripts/engine_probe.py --backend gpu        # 验 CUDA 后端
    python scripts/engine_probe.py --lib <路径> --model <路径>
    python scripts/engine_probe.py --say "用一句话介绍你自己。" --no-think

它会报出**三个**后端：你请求的（`--backend`）、按文件名找到的那份库、以及
**实际跑在哪个后端**（读引擎的 `model_info` —— 这个才是权威答案：CUDA 版多返回
`engine` / `backend` 两个键，CPU 版 0.4.1 没有）。三者不一致时会明确说出来 ——
"选了 GPU 其实是 CPU"是这条路上最容易让人白排查半天的事。

退出码：0 = 可用；1 = 库与权重都在但用不了（生成失败、库版本不匹配）；
2 = 库或权重缺失（游戏会自动退回脚本化兜底，**不是故障**）。
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 的 Python 默认按系统代码页编码 stdout（英文版是 cp1252），中文会直接抛
# UnicodeEncodeError。这个脚本正是给玩家在 Windows 上跑的，所以显式转 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from server import engine as engine_mod  # noqa: E402
from server.llm import build_provider  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=engine_mod.BACKENDS, default="cpu",
                    help="要验哪个后端：cpu（默认）/ gpu（CUDA）")
    ap.add_argument("--lib", default="", help="引擎库路径（默认按后端自动探测）")
    ap.add_argument("--model", default="", help="Qwen3 GGUF 路径（默认自动探测）")
    ap.add_argument("--context", type=int, default=1024, help="会话上下文容量")
    ap.add_argument("--max-tokens", type=int, default=48)
    ap.add_argument("--say", default="用一句话介绍你自己。")
    ap.add_argument("--think", dest="think", action="store_true", help="开启思考模式")
    ap.add_argument("--no-think", dest="think", action="store_false")
    ap.set_defaults(think=False)
    args = ap.parse_args()

    lib = engine_mod.find_lib(args.lib, args.backend)
    weights = engine_mod.find_model(args.model)
    print("请求后端:", args.backend)
    print("引擎库  :", lib or "（没找到）")
    print("权重    :", weights or "（没找到）")
    if lib is None or weights is None:
        print("\n内置引擎不可用 —— 游戏会退回脚本化兜底，这不是故障。")
        if args.backend == "gpu":
            print("GPU 版要接 CUDA 仓库的产物：")
            print("  python scripts/sync_engine.py --backend gpu"
                  "            # 或 --lib <产物路径>")
        else:
            print("把 rpg_engine.dll 与 Qwen3-*.gguf 放进 models/，或用 --lib / --model 指定。")
        return 2

    lib_backend = engine_mod.lib_backend_hint(lib)
    if lib_backend != args.backend:
        print(f"  [!] 请求 {args.backend}，按文件名找到的是 {lib_backend} 版"
              f"（{'GPU 库缺失，已退回 CPU' if args.backend == 'gpu' else '两者不一致'}）")

    provider = build_provider({
        "kind": "engine", "lib": str(lib), "model": str(weights), "backend": args.backend,
        "context": args.context, "max_tokens": args.max_tokens, "think": args.think,
    })
    if provider is None:
        print("\n构建 Provider 失败（库与权重都在，多半是库版本不匹配）。")
        return 1

    lib_obj = engine_mod.open_lib(lib)
    model = lib_obj.model_for(weights, threads=0, max_pos=args.context)
    info = lib_obj.model_info(model)
    actual = engine_mod.backend_from_info(info)     # ← 权威答案：读的是引擎自己报的
    print("引擎版本:", lib_obj.version())
    print("实际后端:", actual or "（引擎没报 —— 老版本？）")
    if actual == "host-only":
        print("  [x] 这份库是 **CUDA 仓库的宿主-only 构建**（没编 CUDA 后端）："
              "元数据能读，**生成会失败**。")
        print("      → Windows 上编 CUDA 要 MSVC（nvcc 不认 g++）；Linux 上要有 nvcc。")
    elif actual == "gpu":
        dev = {k: info[k] for k in ("device", "device_bytes", "device_available")
               if k in info}
        if dev:
            print("设备    :", json.dumps(dev, ensure_ascii=False))
    elif args.backend == "gpu":
        print("  [!] 请求了 GPU，但这份库报的是 CPU 后端 —— 装错产物了？")
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

    try:
        asyncio.run(run())
    except engine_mod.EngineError as e:
        # 宿主-only 构建（没编 CUDA 后端）就是在这里露头的：元数据读得到、
        # 生成报错。给一句人话，别让玩家对着 traceback 猜。
        print(f"\n[x] 生成失败：{e}")
        if actual == "host-only":
            print("    这正是「CUDA 仓库的宿主-only 构建」的表现 —— 见上面的提示："
                  "这份库没有连上 CUDA 后端。")
        return 1
    print("\n自检通过：内置引擎可用"
          + ("（GPU 后端）" if actual == "gpu" else "（CPU 后端）") + "。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
