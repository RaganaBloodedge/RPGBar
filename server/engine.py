"""内置推理引擎（`rpg_engine.dll`）的 ctypes 绑定。

薄薄一层：找库 / 加载 / 十来个 C 调用。**不含任何 Agent 逻辑** ——
Provider 形态在 `llm.py` 的 `EngineProvider`，Agent 语义在 `agents/`。

引擎侧的设计（见 RPGBarEngine 仓库的 `docs/DESIGN.md`）：

- **两级句柄**：`model` 是权重（一个进程通常只加载一份），`session` 是独立推理
  上下文（自己的 KV Cache 与 token 历史）。DM 与小助手**共用权重、不共用会话** ——
  这正是"同一份权重、两个互不干扰的上下文"所必需的。
- **宿主每轮传完整 messages**：引擎自己做最长公共前缀匹配，只对新增的部分做前向。
  所以这里**不需要**替引擎记住对话历史，每轮把 messages 拼好直接传即可
  （拼装由 `agents/` 负责）。
- **种子是会话属性**：引擎不产生随机性，随机数由宿主给的 `seed` 按 KV 绝对位置
  展开（`u = f(seed, 位置)`）。`derive_seed()` 把「本局种子 + 会话标签」派生成各
  会话的独立种子；**不给种子就会落到引擎默认 0** —— 那样每个房间、每一局的随机流
  都完全一样，"换个房间重开"其实是同一串随机数。
- **异常不穿 C ABI**：失败时返回负数 / 空指针，错误文本走 `rpg_engine_last_error()`。

本模块的定位是"**任何一台机器上都能静默缺席**"：库或权重找不到时
`build_provider()` 直接返回 None，游戏退回脚本化兜底（与"引擎加载失败 → 视为 off"
是同一条降级链）。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import sys
import threading
from pathlib import Path

from .config import RESOURCE_DIR, USER_DIR


class EngineError(Exception):
    """引擎不可用或调用失败（库/权重缺失、ABI 报错、返回非法 JSON）。"""


# ---------------------------------------------------------------- 随机种子

# 引擎的 seed 是无符号 64 位。房间种子只掷 63 位 —— 留一位余量，免得将来某个
# 环节把它当有符号数（JSON / 某些语言的 int）时变成负数。
MAX_SEED = (1 << 64) - 1


def derive_seed(room_seed: int, label: str) -> int:
    """由「本局种子 + 会话标签」派生一个独立的 64 位种子。

    为什么要派生而不是直接共用：引擎是纯函数 —— 同一个 seed、同一段 KV 位置
    → 同一个随机数 u。DM 与小助手共用同一份权重、各持一个独立会话，如果两边
    拿同一个 seed，两边在相同位置就会抽到同一个 u。实际影响很小（两边提示词与
    分布都不同），但派生一次是免费的，而且让"小助手也有创造性、但不与 DM 同
    一条随机流"这件事在代码里显式。

    用 blake2b 而**不是**内置 `hash()`：内置 hash 对 str 是按进程加盐的
    （PYTHONHASHSEED），拿它当种子的话，"日志里记下的那个种子"换个进程就复现
    不出来 —— 而可复现正是这个函数存在的全部理由。
    """
    key = (int(room_seed) & MAX_SEED).to_bytes(8, "big")
    digest = hashlib.blake2b(label.encode("utf-8"), digest_size=8, key=key).digest()
    return int.from_bytes(digest, "big")


# ---------------------------------------------------------------- 找库 / 找权重

def _lib_filenames() -> list[str]:
    if sys.platform == "win32":
        return ["rpg_engine.dll"]
    if sys.platform == "darwin":
        return ["librpg_engine.dylib", "rpg_engine.dylib"]
    return ["librpg_engine.so", "rpg_engine.so"]


def _search_dirs() -> list[Path]:
    """找东西的顺序：可写目录在前（用户自己放的优先），只读资源目录在后。"""
    return [USER_DIR / "models", RESOURCE_DIR / "models", USER_DIR, RESOURCE_DIR]


def find_lib(explicit: str = "") -> Path | None:
    """找引擎动态库。

    `explicit` 非空时**只用它**（写明路径却找不到，说明配置有问题，不该偷偷换
    一个能加载的库继续跑 —— 那种"能用但不是你以为的那个"最难查）。
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for name in _lib_filenames():
        for d in _search_dirs():
            p = d / name
            if p.is_file():
                return p
    return None


def find_model(explicit: str = "") -> Path | None:
    """找权重（GGUF）。不指定时挑 `models/` 下的第一个 `Qwen3-*.gguf`。"""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for d in _search_dirs():
        if not d.is_dir():
            continue
        for pattern in ("Qwen3-*.gguf", "*.gguf"):
            hits = sorted(d.glob(pattern))
            if hits:
                return hits[0]
    return None


# ---------------------------------------------------------------- 库句柄

class EngineLib:
    """一份动态库。

    进程内同一个库**只该加载一次** —— 库自己持全局的模型/会话注册表，重复加载会
    得到两套互不可见的注册表（句柄串号）。用 `open_lib()` 拿实例。
    """

    def __init__(self, lib_path: str | Path):
        self.path = str(lib_path)
        try:
            self._lib = ctypes.CDLL(self.path)
        except OSError as e:
            raise EngineError(f"加载引擎库失败：{e}") from e
        self._bind()
        self._lock = threading.Lock()
        self._models: dict[str, int] = {}

    def _bind(self) -> None:
        """声明每个入口的签名。

        少了这一步，ctypes 会按 int 猜参数、按 int 猜返回值 —— 在 64 位下
        **指针会被截断成 32 位**，症状是随机崩溃而不是报错。所以一个都不能省。
        """
        L = self._lib
        try:
            L.rpg_engine_version.restype = ctypes.c_char_p
            L.rpg_engine_last_error.restype = ctypes.c_char_p

            L.rpg_engine_load_model.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            L.rpg_engine_load_model.restype = ctypes.c_int32
            L.rpg_engine_unload_model.argtypes = [ctypes.c_int32]
            L.rpg_engine_unload_model.restype = None

            L.rpg_engine_session_create.argtypes = [ctypes.c_int32, ctypes.c_char_p]
            L.rpg_engine_session_create.restype = ctypes.c_int32
            L.rpg_engine_session_destroy.argtypes = [ctypes.c_int32]
            L.rpg_engine_session_destroy.restype = None
            L.rpg_engine_session_reset.argtypes = [ctypes.c_int32]
            L.rpg_engine_session_reset.restype = ctypes.c_int32

            # 返回的是引擎分配的内存 → 用 void_p 接住原始指针，交给 rpg_engine_free。
            # 若声明成 c_char_p，ctypes 会把它转成 bytes 并丢掉指针，那块内存就漏了。
            L.rpg_engine_generate.argtypes = [ctypes.c_int32, ctypes.c_char_p]
            L.rpg_engine_generate.restype = ctypes.c_void_p
            L.rpg_engine_free.argtypes = [ctypes.c_void_p]
            L.rpg_engine_free.restype = None

            L.rpg_engine_model_info.argtypes = [ctypes.c_int32]
            L.rpg_engine_model_info.restype = ctypes.c_void_p
        except AttributeError as e:
            raise EngineError(
                f"引擎库缺少入口：{e}（{self.path}）—— 库与宿主版本不匹配？"
            ) from e

    # ---- 元信息 ----

    def version(self) -> str:
        v = self._lib.rpg_engine_version()
        return v.decode("utf-8", "replace") if v else ""

    def last_error(self) -> str:
        e = self._lib.rpg_engine_last_error()
        return e.decode("utf-8", "replace") if e else ""

    def _take_json(self, ptr) -> dict:
        """取走引擎返回的 JSON 并释放它（那块内存只能由引擎自己释放）。"""
        if not ptr:
            raise EngineError(self.last_error() or "引擎返回了空指针且没有错误信息")
        try:
            raw = ctypes.string_at(ptr).decode("utf-8", "replace")
        finally:
            self._lib.rpg_engine_free(ptr)
        try:
            return json.loads(raw)
        except ValueError as e:
            raise EngineError(f"引擎返回的不是合法 JSON：{raw[:200]}") from e

    # ---- 模型 ----

    def load_model(self, model_path: str | Path, threads: int = 0,
                   max_pos: int = 4096, max_seq: int = 0) -> int:
        cfg = json.dumps({"threads": int(threads), "max_pos": int(max_pos),
                          "max_seq": int(max_seq)})
        h = self._lib.rpg_engine_load_model(
            str(model_path).encode("utf-8"), cfg.encode("utf-8")
        )
        if h < 0:
            raise EngineError(
                f"加载权重失败（{Path(model_path).name}）：{self.last_error()}"
            )
        return h

    def model_for(self, model_path: str | Path, **kw) -> int:
        """按**绝对路径**缓存：同一份权重在进程内只加载一次。

        加载动作**放在锁内**：把它放锁外（先查、miss 了再慢慢加载）会让两个线程
        同时 miss、同时 `load_model` 同一份 GGUF —— 白读 1 GB，而且会往引擎的模型
        注册表里塞进两个句柄（后写的覆盖前者，先加载那份就此泄漏）。加载期间持锁
        是正确的取舍：本来也只应该发生一次。
        """
        key = str(Path(model_path).expanduser().resolve())
        with self._lock:
            h = self._models.get(key)
            if h is None:
                h = self.load_model(key, **kw)
                self._models[key] = h
            return h

    def unload_model(self, model: int) -> None:
        with self._lock:
            for key, h in list(self._models.items()):
                if h == model:
                    del self._models[key]
        self._lib.rpg_engine_unload_model(ctypes.c_int32(model))

    def model_info(self, model: int) -> dict:
        return self._take_json(self._lib.rpg_engine_model_info(ctypes.c_int32(model)))

    def loaded_models(self) -> list[int]:
        with self._lock:
            return list(self._models.values())

    # ---- 会话 ----

    def new_session(self, model: int, max_seq: int = 0, max_tokens: int = 512,
                    stop: list[str] | None = None, seed: int | None = None) -> int:
        cfg: dict = {"max_seq": int(max_seq), "max_tokens": int(max_tokens)}
        if stop:
            cfg["stop"] = [str(s) for s in stop]
        if seed is not None:
            # 种子的落点是**会话**而不是每轮请求：本局固定，说一次就够。
            # （引擎侧仍按"请求 > 会话"取值，将来要单轮换种子也不用改这里。）
            cfg["seed"] = int(seed) & MAX_SEED
        h = self._lib.rpg_engine_session_create(
            ctypes.c_int32(model), json.dumps(cfg, ensure_ascii=False).encode("utf-8")
        )
        if h < 0:
            raise EngineError(f"创建会话失败：{self.last_error()}")
        return h

    def destroy_session(self, session: int) -> None:
        self._lib.rpg_engine_session_destroy(ctypes.c_int32(session))

    def reset_session(self, session: int) -> bool:
        return self._lib.rpg_engine_session_reset(ctypes.c_int32(session)) >= 0

    def generate(self, session: int, request: dict) -> dict:
        """跑一轮生成。**阻塞**（CPU 上几十秒），调用方自己决定要不要丢到线程里。"""
        buf = json.dumps(request, ensure_ascii=False).encode("utf-8")
        return self._take_json(
            self._lib.rpg_engine_generate(ctypes.c_int32(session), buf)
        )


_libs: dict[str, EngineLib] = {}
_libs_lock = threading.Lock()


def open_lib(lib_path: str | Path) -> EngineLib:
    """按路径取库实例（进程内同一个库只加载一次）。"""
    key = str(Path(lib_path).expanduser().resolve())
    with _libs_lock:
        lib = _libs.get(key)
        if lib is None:
            lib = EngineLib(key)
            _libs[key] = lib
        return lib


# ---------------------------------------------------------------- 生成锁

# **同一份权重同一时刻只允许一个 generate。**
#
# 为什么是"按权重"而不是"按会话"：引擎里一个 model 持有一份共享的线程池，多个
# session 的 generate 会往同一个池子里提交任务。实测两个房间各自点一次「助手建议」
# （两个会话、同一份权重、并发 generate），服务器进程会**无声消失** —— 日志停在
# 「已创建会话 #2」，没有 Python 异常、没有 traceback（典型的堆/资源竞争崩溃）。
# 在 CPU 上并发跑本来也没有收益（只会互相抢核），串行化是唯一正确的语义。
#
# 宿主侧的 `EngineProvider` 原先只有一把**实例级**锁（`self._busy`）—— 一个房间一个
# Provider，多房间时这把锁形同虚设。这里改成"按权重路径共享"，才是真正串行。
_model_locks: dict[str, threading.Lock] = {}
_model_locks_guard = threading.Lock()


def model_lock(model_path: str | Path) -> threading.Lock:
    """取「这份权重」的全局生成锁（同路径共享同一把）。"""
    key = str(Path(model_path).expanduser().resolve())
    with _model_locks_guard:
        lk = _model_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _model_locks[key] = lk
        return lk
