#!/usr/bin/env python
"""下载剧情档案用的嵌入模型 bge-small-zh-v1.5（ONNX 版），放到 models/ 目录。

为什么单独做一个脚本：模型文件 24MB（量化）/ 95MB（fp32），不适合进版本库。
一次性下好之后，`server/embedding.py` 会自动发现它；打包时把 models/ 目录
一并塞进发行包即可（PyInstaller 的 --add-data）。

用法：
    python scripts/fetch_embedding.py              # 默认下量化版（~24MB）
    python scripts/fetch_embedding.py --fp32       # 下完整 fp32 版（~95MB，更准）
    python scripts/fetch_embedding.py --source hf  # 直连 HuggingFace（默认先试镜像）
    python scripts/fetch_embedding.py --dir D:/models/bge-small-zh-v1.5

没下也没关系：服务器检测不到模型会自动退化成纯 BM25 检索，游戏照常玩。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = "Xenova/bge-small-zh-v1.5"
SOURCES = {
    "mirror": f"https://hf-mirror.com/{REPO}/resolve/main",
    "hf": f"https://huggingface.co/{REPO}/resolve/main",
}
# (远端路径, 本地文件名)
FILES_COMMON = [("tokenizer.json", "tokenizer.json")]
FILES_QUANT = [("onnx/model_quantized.onnx", "model_quantized.onnx")]
FILES_FP32 = [("onnx/model.onnx", "model.onnx")]


def _download(url: str, dest: Path) -> bool:
    try:
        import httpx
    except ImportError:
        print("需要 httpx：pip install httpx", file=sys.stderr)
        return False
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  下载 {url}")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        print(f"\r    {pct:3d}%  {done / 1e6:6.1f}/{total / 1e6:.1f} MB", end="")
        print()
        tmp.replace(dest)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  失败：{exc}", file=sys.stderr)
        if tmp.exists():
            tmp.unlink()
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 bge-small-zh-v1.5（ONNX）")
    ap.add_argument("--dir", default=None, help="目标目录（默认 <项目>/models/bge-small-zh-v1.5）")
    ap.add_argument("--fp32", action="store_true", help="下载完整 fp32 版（更大更准）")
    ap.add_argument("--source", choices=["auto", "mirror", "hf"], default="auto", help="下载源")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    dest = Path(args.dir) if args.dir else (root / "models" / "bge-small-zh-v1.5")
    dest.mkdir(parents=True, exist_ok=True)
    files = FILES_COMMON + (FILES_FP32 if args.fp32 else FILES_QUANT)

    order = ["hf", "mirror"] if args.source == "hf" else (["mirror", "hf"] if args.source == "auto" else ["mirror"])
    print(f"[fetch_embedding] 目标目录：{dest}")
    for remote, local in files:
        target = dest / local
        if target.exists() and target.stat().st_size > 0:
            print(f"  已存在，跳过：{local}")
            continue
        ok = False
        for src in order:
            print(f"[fetch_embedding] {local} ← {src}")
            if _download(f"{SOURCES[src]}/{remote}", target):
                ok = True
                break
        if not ok:
            print(f"[fetch_embedding] 无法下载 {local}", file=sys.stderr)
            return 1

    print("[fetch_embedding] 完成。启动服务器时会看到「剧情档案：已启用（bge-small-zh-v1.5 512 维 + BM25 混合检索）」。")
    print("[fetch_embedding] 打包时记得把 models/ 一起带进发行包。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
