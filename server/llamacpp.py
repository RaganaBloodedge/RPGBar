"""llama.cpp 接入层：把 `llama-server.exe` 作为**子进程**拉起，走 OpenAI 兼容 HTTP。

为什么是「子进程 + HTTP」而不是 ctypes 直绑 llama.dll：

- llama.cpp 的 C API 是"整条推理流水线"（几百个入口，batch/sampler/decode loop 都
  要宿主自己管）。直绑等于用 Python 重写一遍 llama-server。
- `llama-server` 就是官方维护的 OpenAI 兼容服务：工具调用（`--jinja`）、跨请求
  前缀 KV 复用（同 slot 的最长公共前缀自动续用）、并发槽位（`-np`）都是现成的。
- **进程边界 = 崩溃边界**：模型相关的一切崩溃都落在子进程里，不会带走游戏服务器
  （自研引擎时代"两个会话并发 generate → 进程无声消失"的教训，这里结构性规避）。

生命周期与所有权（与 `engine.py` 的两级句柄对齐）：

- 一个 `(exe, model, backend, context, threads)` 组合 = **一个** llama-server 进程，
  全进程共享（assistant 与 advisor 同一份 1.7B 同为 CPU → 共用一个 server；
  DM 的 4B GPU 是另一个）。
- Provider 持**引用计数**：`acquire()` 建进程，`release()` 归零时才终止。
- 请求级并发：`-np 1`（默认）时请求在 server 内排队 —— 与自研引擎
  「同一份权重串行 generate」语义一致，多房间并发也不会把显存打爆。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import RESOURCE_DIR, USER_DIR

# 与 engine.py 同一条哲学：这份库"任何一台机器上都能静默缺席"。
# 找不到 llama-server.exe → build_provider 返回 None → 槽位没有模型，显式失败。


def find_llama_server(explicit: str = "") -> Path | None:
    """找 llama-server 可执行文件。

    搜索顺序：槽位显式路径（写明就只用它，找不到算配置错）→
    本仓库 `tools/src/llama.cpp-*/build/bin/`（官方 release 解包的标准布局）→
    可写用户目录 → 只读资源目录。
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    stems = ["llama-server.exe"] if sys.platform.startswith("win") else ["llama-server"]
    dirs: list[Path] = []
    # 仓库内随包的 llama.cpp（tools/src/llama.cpp-b11819/build/bin/…）
    tools_dir = RESOURCE_DIR / "tools" / "src"
    if tools_dir.is_dir():
        for d in sorted(tools_dir.glob("llama.cpp-*/build/bin")):
            dirs.append(d)
    dirs += [USER_DIR, RESOURCE_DIR]
    for stem in stems:
        for d in dirs:
            p = d / stem
            if p.is_file():
                return p
    return None


def _free_port() -> int:
    """向操作系统要一个空闲 TCP 端口（绑定后立刻释放，存在理论竞争窗口，
    实际上 llama-server 启动要几秒，被抢走的概率可以忽略；万一撞了，
    /health 等不到、报错信息里带端口，一眼能看出来）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def server_args(model_path, port: int, *, ngl: int, context: int,
                threads: int = 0, np: int = 1) -> list[str]:
    """拼 llama-server 启动参数。

    - `--jinja`：Qwen3 的工具调用要靠官方 chat template 渲染（没有它
      /v1/chat/completions 的 tools 参数会被忽略）。
    - `-ngl`：gpu=99（全部层上卡）/ cpu=0 —— 与自研引擎的 backend 二值对齐。
    - `-c`：上下文容量；`-t`：CPU 线程（GPU 后端传 0 = 不限制，反正不靠 CPU 算）。
    - `-np`：并发槽位数。1 = 请求排队（省 KV 显存），与"同一权重串行生成"同语义。
    """
    return [
        "-m", str(model_path),
        "--host", "127.0.0.1",
        "--port", str(int(port)),
        "-c", str(int(context)),
        "-ngl", str(int(ngl)),
        "-np", str(int(np)),
        "-t", str(int(threads)),
        "--jinja",
    ]


class LlamaServerHandle:
    """一个跑起来的 llama-server 子进程（带引用计数）。"""

    def __init__(self, exe: Path, model_path: Path, *, ngl: int, context: int,
                 threads: int = 0, np: int = 1):
        self.exe = Path(exe).resolve()
        # 🔴 必须转绝对路径：子进程 cwd 是 llama-server 自己的 bin 目录，
        # 相对路径（config 里的 "models/xxx.gguf"）在它眼里不存在（实测踩过）。
        self.model_path = Path(model_path).expanduser().resolve()
        self.ngl = int(ngl)
        self.context = int(context)
        self.threads = int(threads)
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._args = server_args(self.model_path, self.port, ngl=ngl, context=context,
                                 threads=threads, np=np)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._refs = 0
        self._log_path: Path | None = None
        self._log_file = None
        # 子进程日志落盘：模型加载失败 / 端口冲突都写在它的 stdout 里，
        # 服务器侧只能看到"health 超时"，排障全靠这份日志。
        try:
            log_dir = USER_DIR / "data" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_dir = log_dir
        except OSError:
            self._log_dir = None

    def _open_log(self):
        """（重）开日志文件。为什么不能只在 __init__ 开一次：release() 终止进程时会
        关掉它，而句柄是跨房间复用的 —— 下一个房间 start() 再 spawn 时会往**已关闭
        的文件**里写（实测报 I/O operation on closed file）。所以每次 start 都确保
        它是开着的。"""
        if self._log_dir is None:
            return None
        if self._log_file is None or self._log_file.closed:
            self._log_path = self._log_dir / f"llamacpp-{self.port}.log"
            try:
                self._log_file = open(self._log_path, "ab")
            except OSError:
                self._log_file = None
        return self._log_file

    def start(self, timeout: float = 180.0) -> None:
        """拉起子进程并等 /health 就绪（模型加载完才算就绪）。

        模型加载：4B 上卡 ~7-9 s、1.7B 读盘 ~2 s，冷盘更慢，超时给足 180 s。
        进程半路死了 → 立刻报错，不空等（错误文本带上日志路径）。
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            # 引用计数归零时 release() 会终止进程；句柄是跨房间复用的，
            # 所以这里要能"死而复生"——重新 spawn（日志文件也重开）。
            self._open_log()
            logf = self._log_file if (self._log_file and not self._log_file.closed) else None
            self._proc = subprocess.Popen(
                [str(self.exe), *self._args],
                stdout=logf or subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                cwd=str(self.exe.parent),
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    raise RuntimeError(
                        f"llama-server 启动即退出（码 {self._proc.returncode}）——"
                        f"参数 {' '.join(self._args)}"
                        + (f"，日志 {self._log_path}" if self._log_path else "")
                    )
                if self._health_ok():
                    return
                time.sleep(0.3)
            raise RuntimeError(
                f"llama-server 就绪超时（{timeout:.0f}s）—— 模型太大 / 磁盘太慢？"
                f"端口 {self.port}"
                + (f"，日志 {self._log_path}" if self._log_path else "")
            )

    def _health_ok(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=2) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            return e.code == 200  # 加载中是 503
        except Exception:  # noqa: BLE001  连不上 = 还没起来
            return False

    def acquire(self) -> None:
        with self._lock:
            self._refs += 1

    def release(self) -> None:
        """引用归零才真正终止子进程（Provider.close 只减计数）。"""
        with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs > 0:
                return
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass


_handles: dict[tuple, LlamaServerHandle] = {}
_handles_lock = threading.Lock()


def open_server(exe, model_path, *, backend: str = "gpu", context: int = 6144,
                threads: int = 0) -> LlamaServerHandle:
    """按 (exe, model, backend, context, threads) 取共享的 server 句柄（不存在则建）。

    共享键里含 context：ctx 不同 → `-c` 不同 → KV 大小不同，不能混用。
    """
    from .engine import normalize_backend

    be = normalize_backend(backend, default="gpu")
    ngl = 99 if be == "gpu" else 0
    key = (str(Path(exe).resolve()), str(Path(model_path).resolve()),
           be, int(context), int(threads))
    with _handles_lock:
        h = _handles.get(key)
        if h is None:
            h = LlamaServerHandle(exe, model_path, ngl=ngl, context=context,
                                  threads=threads)
            _handles[key] = h
        h.acquire()
        return h


def shutdown_all() -> None:
    """进程退出前把所有 llama-server 子进程收干净（main.py 挂 atexit）。"""
    with _handles_lock:
        items = list(_handles.values())
        _handles.clear()
    for h in items:
        try:
            while True:
                h.release()
                with h._lock:
                    if h._refs == 0:
                        break
        except Exception:  # noqa: BLE001  退出路径不冒异常
            pass
