#!/usr/bin/env python3
"""把 RPGBarEngine 的产物同步到本项目的 models/ —— 供"内置引擎"通道使用。

为什么需要这个脚本
------------------
内置引擎通道要求动态库与权重**在搜索路径里**（`server/engine.py::_search_dirs`）：
`models/`（本项目）与前一级目录。引擎的产物却在另一个仓库（`RPGBarEngine/build`
与 `RPGBarEngine/models`），所以要"接"过来。

用硬链接而不是复制
------------------
权重是 1 GB 量级（Q4_K_M 1.03 GiB / Q8_0 1.71 GiB），复制一份纯属浪费。
默认用**硬链接**：同一磁盘卷内零额外空间、零拷贝，改一处两边都变。
跨盘或文件系统不支持时自动回退为复制（会提示）。

用法
----
    python scripts/sync_engine.py                      # 自动找 ../RPGBarEngine
    python scripts/sync_engine.py --engine-repo D:/RPGBarEngine
    python scripts/sync_engine.py --all                # 同步全部 Qwen3-*.gguf
    python scripts/sync_engine.py --copy               # 强制复制（跨盘/网络盘时用）

同步完可以直接自检：`python scripts/engine_probe.py`
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 本项目根
DEST = ROOT / "models"

LIB_NAMES = ("rpg_engine.dll", "librpg_engine.so", "librpg_engine.dylib",
             "rpg_engine.so", "rpg_engine.dylib")


def find_engine_repo(explicit: str | None) -> Path | None:
    """找引擎仓库：显式路径优先，否则试同级目录 ../RPGBarEngine。"""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.is_dir() else None
    for cand in (ROOT.parent / "RPGBarEngine", ROOT / "RPGBarEngine"):
        if cand.is_dir():
            return cand
    return None


def pick_lib(repo: Path) -> Path | None:
    """在仓库里找动态库产物（先 build/，再根目录）。"""
    for d in (repo / "build", repo):
        if not d.is_dir():
            continue
        for name in LIB_NAMES:
            p = d / name
            if p.is_file():
                return p
    return None


def pick_weights(repo: Path, take_all: bool) -> list[Path]:
    """找权重：默认只挑一个（Q4_K_M 优先 —— 体积与质量的平衡点）。"""
    d = repo / "models"
    if not d.is_dir():
        return []
    hits = sorted(d.glob("Qwen3-*.gguf"))
    if take_all or not hits:
        return hits
    prefer = [p for p in hits if "Q4_K_M" in p.name]
    return prefer[:1] or hits[:1]


def _already_linked(src: Path, dst: Path) -> bool:
    """dst 是否已经是 src 的硬链接（同一 inode）。"""
    try:
        return os.path.samefile(src, dst)
    except OSError:
        return False


def place(src: Path, dst: Path, force_copy: bool) -> tuple[str, str]:
    """把 src 放到 dst。返回 (动作, 说明)。"""
    if dst.exists():
        if _already_linked(src, dst):
            return "skip", "已是指向引擎仓库的硬链接"
        # 已存在但来源不同：换掉它（用户显式跑同步 = 想让它同步）
        try:
            dst.unlink()
        except OSError as e:
            return "fail", f"目标已存在且无法替换：{e}"

    if not force_copy:
        try:
            os.link(src, dst)
            return "link", "硬链接"
        except OSError:
            pass  # 跨盘 / 文件系统不支持 → 落到复制
    try:
        shutil.copy2(src, dst)
        return "copy", "复制"
    except OSError as e:
        return "fail", str(e)


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return str(n)


def main() -> int:
    ap = argparse.ArgumentParser(description="同步引擎产物到 models/")
    ap.add_argument("--engine-repo", default=None,
                    help="引擎仓库路径（默认自动找 ../RPGBarEngine）")
    ap.add_argument("--all", action="store_true",
                    help="同步全部 Qwen3-*.gguf（默认只挑一个）")
    ap.add_argument("--copy", action="store_true",
                    help="强制复制而不是硬链接")
    args = ap.parse_args()

    repo = find_engine_repo(args.engine_repo)
    if repo is None:
        print("[x] 找不到引擎仓库。用 --engine-repo 指定，例如：")
        print("      python scripts/sync_engine.py --engine-repo D:/RPGBarEngine")
        return 2

    print(f"引擎仓库：{repo}")
    print(f"目标目录：{DEST}")
    lib = pick_lib(repo)
    weights = pick_weights(repo, args.all)
    if lib is None and not weights:
        print("[x] 仓库里既没找到动态库产物，也没找到 Qwen3-*.gguf。")
        print("    先在引擎仓库跑一次构建（build.sh / build.bat）。")
        return 2

    DEST.mkdir(parents=True, exist_ok=True)
    failures = 0
    for src in ([lib] if lib else []) + weights:
        if src is None:
            continue
        dst = DEST / src.name
        action, note = place(src, dst, args.copy)
        size = human(src.stat().st_size)
        mark = {"skip": "=", "link": "+", "copy": "+", "fail": "x"}[action]
        print(f"  [{mark}] {src.name:<28} {size:>10}  {note}")
        if action == "fail":
            failures += 1

    if lib is None:
        print("  [!] 没找到动态库产物 —— 先构建引擎仓库。")
    if not weights:
        print("  [!] 没找到 Qwen3-*.gguf —— 先下载权重。")

    print()
    if failures:
        print(f"完成，但有 {failures} 个文件失败。")
        return 1
    print("完成。自检：python scripts/engine_probe.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
