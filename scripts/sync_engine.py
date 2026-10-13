#!/usr/bin/env python3
"""把 RPGBarEngine 的产物同步到本项目的 models/ —— 供"内置引擎"通道使用。

两个后端，两份产物
------------------
内置引擎有**两份实现、同一个 C ABI**：

- CPU 版：`RPGBarEngine`（仓库同级目录 `../RPGBarEngine`）→ `rpg_engine.dll`
- GPU 版：`RPGBarEngineCUDA`（`../RPGBarEngineCUDA`）→ 同步时**改名为**
  `rpg_engine_cuda.dll`

为什么要改名：游戏里那个「推理后端」下拉框只能"在这张枚举好的名单里挑"
（`server/engine.py::_LIB_STEMS`），客户端指不了任意 dll —— 与"界面不许改
lib / model"是同一条安全边界。文件名就是后端。

⚠️ 改名只发生在**同步这一步**，另一个仓库的构建产物名不动（那边仍是
`rpg_engine.dll` / `librpg_engine.so`）—— 那边的"同一个 ABI"故事也就不用改。

为什么需要这个脚本
------------------
内置引擎通道要求动态库与权重**在搜索路径里**（`server/engine.py::_search_dirs`）：
`models/`（本项目）与前一级目录。引擎的产物却在另一个仓库，所以要"接"过来。

用硬链接而不是复制
------------------
权重是 1 GB 量级（Q4_K_M 1.03 GiB / Q8_0 1.71 GiB），复制一份纯属浪费。
默认用**硬链接**：同一磁盘卷内零额外空间、零拷贝，改一处两边都变。
跨盘或文件系统不支持时自动回退为复制（会提示）。

用法
----
    python scripts/sync_engine.py                  # 两个后端都接（找不到的那个会提示）
    python scripts/sync_engine.py --backend cpu    # 只接 CPU 版
    python scripts/sync_engine.py --backend gpu    # 只接 GPU 版（CUDA）
    python scripts/sync_engine.py --lib ~/cuda-build/librpg_engine.so   # GPU 库直接指路径
    python scripts/sync_engine.py --cuda-repo D:/RPGBarEngineCUDA
    python scripts/sync_engine.py --all            # 同步全部 Qwen3-*.gguf
    python scripts/sync_engine.py --copy           # 强制复制（跨盘/网络盘时用）

⚠️ **WSL 里构建的 CUDA 产物在 `~/cuda-build`，不在仓库的 `build/` 里**（那边只
留源码、构建目录落在 ext4 上），所以 Linux 下多半要用 `--lib` 直接指过去。

同步完务必自检（它会报出**实际**后端）：
    python scripts/engine_probe.py --backend gpu
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

# 后端 → 引擎仓库的目录名（同级目录下，可用 --engine-repo / --cuda-repo 覆盖）
REPO_NAMES = {"cpu": "RPGBarEngine", "gpu": "RPGBarEngineCUDA"}


def gpu_dest_name(name: str) -> str:
    """CUDA 仓库的产物名 → 本项目里的名字（加 `_cuda`）。

    `rpg_engine.dll` → `rpg_engine_cuda.dll`
    `librpg_engine.so` → `librpg_engine_cuda.so`
    """
    p = Path(name)
    return f"{p.stem}_cuda{p.suffix}"


def find_engine_repo(explicit: str | None, which: str = "cpu") -> Path | None:
    """找引擎仓库：显式路径优先，否则试同级目录 `../<REPO_NAMES[which]>`。"""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.is_dir() else None
    name = REPO_NAMES.get(which, REPO_NAMES["cpu"])
    for cand in (ROOT.parent / name, ROOT / name):
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


def _lib_jobs(args) -> tuple[list[tuple[Path, str, str]], list[str]]:
    """要同步哪些库。返回 ([(源文件, 目标文件名, 后端)], [提示行])。

    两份库分开找、分开报错：GPU 版找不到**不影响** CPU 版接好（那 1 GB 权重是
    共用的，装一次两边都能用）。
    """
    jobs: list[tuple[Path, str, str]] = []
    notes: list[str] = []

    if args.backend in ("both", "cpu"):
        repo = find_engine_repo(args.engine_repo, "cpu")
        if repo is None:
            notes.append(f"[!] 没找到 CPU 引擎仓库（{ROOT.parent / REPO_NAMES['cpu']}）——"
                         f" 用 --engine-repo 指定")
        else:
            print(f"CPU 引擎仓库：{repo}")
            lib = pick_lib(repo)
            if lib is None:
                notes.append("[!] CPU 仓库里没有动态库产物 —— 先在那边构建一次"
                             "（build.sh / build.bat）")
            else:
                jobs.append((lib, lib.name, "cpu"))

    if args.backend in ("both", "gpu"):
        if args.lib:
            # 直接指路径：WSL 构建的产物在 ~/cuda-build，不在仓库里，只能这么给
            lib = Path(args.lib).expanduser()
            if not lib.is_file():
                notes.append(f"[x] --lib 指的文件不存在：{lib}")
            else:
                print(f"GPU 引擎库（--lib）：{lib}")
                jobs.append((lib, gpu_dest_name(lib.name), "gpu"))
        else:
            repo = find_engine_repo(args.cuda_repo, "gpu")
            if repo is None:
                notes.append(f"[!] 没找到 CUDA 引擎仓库（{ROOT.parent / REPO_NAMES['gpu']}）——"
                             f" 用 --cuda-repo 指定，或用 --lib 直接指产物")
            else:
                print(f"CUDA 引擎仓库：{repo}")
                lib = pick_lib(repo)
                if lib is None:
                    notes.append(f"[!] {repo} 里没有动态库产物 —— WSL 里构建的话产物在"
                                 f" ~/cuda-build，用 --lib 指过去")
                else:
                    jobs.append((lib, gpu_dest_name(lib.name), "gpu"))

    return jobs, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="同步引擎产物到 models/")
    ap.add_argument("--backend", choices=("both", "cpu", "gpu"), default="both",
                    help="同步哪个后端（默认 both）")
    ap.add_argument("--engine-repo", default=None,
                    help="CPU 引擎仓库路径（默认自动找 ../RPGBarEngine）")
    ap.add_argument("--cuda-repo", default=None,
                    help="CUDA 引擎仓库路径（默认自动找 ../RPGBarEngineCUDA）")
    ap.add_argument("--lib", default="", help="GPU 引擎库直接指路径（跳过仓库查找）")
    ap.add_argument("--all", action="store_true",
                    help="同步全部 Qwen3-*.gguf（默认只挑一个）")
    ap.add_argument("--copy", action="store_true",
                    help="强制复制而不是硬链接")
    args = ap.parse_args()

    jobs, notes = _lib_jobs(args)

    # ---- 权重：两个后端**共用同一份 GGUF**，所以只找一次（优先 CPU 仓库）
    weights: list[Path] = []
    weights_from = ""
    for which in ("cpu", "gpu"):
        repo = find_engine_repo(
            args.engine_repo if which == "cpu" else args.cuda_repo, which)
        if repo is None:
            continue
        got = pick_weights(repo, args.all)
        if got:
            weights, weights_from = got, str(repo)
            break

    if not jobs and not weights:
        print("[x] 既没找到动态库产物，也没找到 Qwen3-*.gguf。")
        for n in notes:
            print("   " + n)
        return 2

    print(f"目标目录：{DEST}")
    if weights_from:
        print(f"权重来源：{weights_from}")

    DEST.mkdir(parents=True, exist_ok=True)
    failures = 0
    for src, dst_name, backend in jobs:
        dst = DEST / dst_name
        action, note = place(src, dst, args.copy)
        size = human(src.stat().st_size)
        mark = {"skip": "=", "link": "+", "copy": "+", "fail": "x"}[action]
        tag = "GPU" if backend == "gpu" else "CPU"
        extra = ""
        if backend == "gpu" and src.name != dst_name:
            extra = f"  （源名 {src.name}）"
        print(f"  [{mark}] {dst_name:<28} {size:>10}  {tag} · {note}{extra}")
        if action == "fail":
            failures += 1

    for src in weights:
        dst = DEST / src.name
        action, note = place(src, dst, args.copy)
        size = human(src.stat().st_size)
        mark = {"skip": "=", "link": "+", "copy": "+", "fail": "x"}[action]
        print(f"  [{mark}] {src.name:<28} {size:>10}  权重 · {note}")
        if action == "fail":
            failures += 1

    for n in notes:
        print("  " + n)

    print()
    if failures:
        print(f"完成，但有 {failures} 个文件失败。")
        return 1
    if not jobs:
        print("只同步了权重：一个动态库都没接上 —— 引擎要库与权重都在才能用。")
    print("自检（**它会报出实际跑在哪个后端**）：")
    print("  python scripts/engine_probe.py --backend cpu")
    print("  python scripts/engine_probe.py --backend gpu     # 接了 CUDA 版就跑这条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
