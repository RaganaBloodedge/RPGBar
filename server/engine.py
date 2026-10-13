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
- **两个后端、同一个 ABI**：CPU 版（`rpg_engine.dll`）与 CUDA 版
  （`rpg_engine_cuda.dll`）是同一份 C 接口的两份实现，所以"用哪个后端"在宿主
  侧就只是"**加载哪一个文件**"（`find_lib(backend=...)`）。选谁由配置 / 游戏内
  设置决定，见下面的「后端」一节。
- **GPU 能不能用要探测，不能靠文件名**：CUDA 版产物随包分发（没独显的机器上
  文件也在），所以 `probe_gpu()` 走"产物在 → 能不能加载 → 驱动报得出设备"三道门，
  给界面一个可置灰的判据 —— 见下面的「GPU 可用性探测」一节。
- **退出前要把权重卸干净**（`unload_all()` 已挂到 `atexit`）：引擎里模型是命名空间
  级静态对象，拖到进程退出才析构时 CUDA 上下文往往已经没了，每次退出刷一屏
  `cudaFree 失败：driver shutting down`。

本模块的定位是"**任何一台机器上都能静默缺席**"：库或权重找不到时
`build_provider()` 直接返回 None，游戏退回脚本化兜底（与"引擎加载失败 → 视为 off"
是同一条降级链）。
"""
from __future__ import annotations

import atexit
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


# ---------------------------------------------------------------- 后端（CPU / GPU）

# 两个后端的**产物文件名**。同一份 C ABI 有两份实现：
#   cpu → RPGBarEngine（纯 CPU，`rpg_engine.dll`）
#   gpu → RPGBarEngineCUDA（CUDA，`rpg_engine_cuda.dll`；ABI 一个函数不差）
#
# 「文件名即后端」是这个功能能做得**安全**的原因：界面上那个「CPU / GPU」选择器
# 只能在这张表里挑，客户端永远指不了一个任意 dll —— 与"界面不许改 lib / model"
# 是同一条边界（见 server/main.py::clean_engine_patch）。
_LIB_STEMS = {"cpu": "rpg_engine", "gpu": "rpg_engine_cuda"}

# 界面上能选的值（服务端白名单，见 main.clean_engine_patch）。
BACKENDS = ("cpu", "gpu")


def normalize_backend(value, default: str = "cpu") -> str:
    """把配置 / 界面传来的值收敛到 BACKENDS 里的一个（认不出来就用默认值）。

    顺手认几个别名（`cuda` / `host`）：配置文件是给人手写的，多认一个同义词比
    让人对着文档查拼写好。但**界面白名单只放 cpu / gpu 两个值**。
    """
    b = str(value or "").strip().lower()
    if b in ("gpu", "cuda"):
        return "gpu"
    if b in ("cpu", "host"):
        return "cpu"
    return default


def _lib_filenames(backend: str = "cpu") -> list[str]:
    stem = _LIB_STEMS.get(normalize_backend(backend), _LIB_STEMS["cpu"])
    if sys.platform == "win32":
        return [f"{stem}.dll"]
    if sys.platform == "darwin":
        return [f"lib{stem}.dylib", f"{stem}.dylib"]
    return [f"lib{stem}.so", f"{stem}.so"]


def _search_dirs() -> list[Path]:
    """找东西的顺序：可写目录在前（用户自己放的优先），只读资源目录在后。"""
    return [USER_DIR / "models", RESOURCE_DIR / "models", USER_DIR, RESOURCE_DIR]


def find_lib(explicit: str = "", backend: str = "cpu") -> Path | None:
    """找引擎动态库。

    `explicit` 非空时**只用它**，且不考虑后端 —— 写明路径的人知道自己要哪一份
    （写明路径却找不到，说明配置有问题，不该偷偷换一个能加载的库继续跑 ——
    那种"能用但不是你以为的那个"最难查）。

    否则按 `backend` 找：

    - `cpu`：只认 CPU 版产物（`rpg_engine.dll`）。**默认值，与加这个开关之前
      的行为逐字相同**（那时候的名单就只有这一个文件）。
    - `gpu`：**先找 CUDA 版产物**（`rpg_engine_cuda.dll`），找不到就**退回 CPU 版**。

    ⚠️ 为什么 `gpu` 要退回而不是"找不到就当作没引擎"：后者会让 `kind=engine`
    的槽位退化成 `off` → DM 变脚本化 → 玩家看到的是"**游戏坏了**"，而不是
    "GPU 不可用"。退回的代价只是慢，而且**退没退是看得见的**：公开状态里的
    `lib_name` 会是 `rpg_engine.dll`，界面据此标出"实际：CPU（GPU 库缺失）"。
    "能玩 + 说实话"优于"诚实但玩不了"。（真不想退回，把 `lib` 写成 CUDA 库的
    绝对路径即可 —— 那条路不退回。）
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    # 请求 gpu 时的尝试顺序：先 CUDA 版，再 CPU 版（见上面的退回说明）。
    order = ["gpu", "cpu"] if normalize_backend(backend) == "gpu" else ["cpu"]
    for b in order:
        for name in _lib_filenames(b):
            for d in _search_dirs():
                p = d / name
                if p.is_file():
                    return p
    return None


def lib_backend_hint(path) -> str:
    """从**文件名**推断这份库是哪个后端（`""` = 没找到库）。

    只是提示：文件名是同步脚本起的名字。**权威来源**是加载后 `model_info` 里的
    `engine` / `backend` 键（见 `backend_from_info`）。
    """
    if not path:
        return ""
    return "gpu" if "cuda" in Path(path).name.lower() else "cpu"


def backend_from_info(info: dict) -> str:
    """从 `model_info` 判断**实际**跑在哪个后端 —— 这是权威答案。

    不需要动 ABI：CUDA 版本来就多两个键（`engine` = `rpg_engine_cuda`、
    `backend` = `cuda` / `host-only`），CPU 版 0.4.1 两个键都没有 —— 于是
    "有没有这两个键"本身就是判别式（见 RPGBarEngineCUDA 的 `docs/DESIGN.md`，
    ABI 只有 10 个函数、一个都不能加）。

    返回值：`"cpu"` / `"gpu"` / `"host-only"`（编了 CUDA 仓库的宿主-only 产物，
    没有 CUDA 后端 —— 能读元数据，生成会明确报错）。
    """
    if not isinstance(info, dict):
        return ""
    eng = str(info.get("engine") or "").strip().lower()
    be = str(info.get("backend") or "").strip().lower()
    if be == "host-only":
        return "host-only"
    if eng.endswith("cuda") or be == "cuda":
        return "gpu"
    return "cpu"


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


# ---------------------------------------------------------------- GPU 可用性探测
#
# 为什么需要它：CUDA 版产物**随包分发**（哪怕对方机器没独显，文件也在），所以
# "文件在不在"不能代表"能不能用 GPU"。界面上的「GPU」选项要据此**置灰**，
# 而不是让玩家选了之后再静默退回 CPU（用户 2026-10-13 拍板）。
#
# ⚠️ 探测**刻意不碰 C ABI**（那 10 个函数是契约，见 `backend_from_info` 的说明）。
# `model_info` 里其实有 `device_available`，但它要**先有模型句柄** —— 而
# `load_model` 在有卡的机器上要 6.6–8.9 秒（把 1.03 GiB 权重搬上卡），拿它做
# "面板一打开就判定"太慢。
#
# 🔴 **实测（2026-10-13，Windows 侧第一次真编出 `.dll` 之后）**：CUDA 版产物把
# cudart **静态链接**进去了 —— `dumpbin /dependents rpg_engine_cuda.dll` 的依赖表里
# 只有 KERNEL32 + VC 运行库 + 几个 `api-ms-win-crt-*`：
#
#     * **没有 cudart** ⇒ 宿主侧拿不到 `cudaGetDeviceCount`（它根本没被导出），
#       原来那版"找 `cudart64_*.dll` 再问它"的探测**会永远判不可用**；
#     * **没有 nvcuda** ⇒ 驱动是**延迟加载**的，所以在没装 N 卡的机器上
#       `LoadLibrary(rpg_engine_cuda.dll)` **不会失败** ——
#       "库能不能加载"与"有没有驱动"是**两件事**，探测里必须分开判。
#
# 所以第 ①③ 道门改问**驱动**（驱动 API 是纯 C 接口，`nvcuda.dll` / `libcuda.so.1`
# 随驱动安装）。
#
# 顺带一个好处：cudart 静态链接 ⇒ 分发时**不用再带 `cudart64_*.dll`**，
# 分发包里 CUDA 相关只有 `rpg_engine_cuda.dll` 一个文件。

# CUDA 驱动库名（驱动 API 的入口；Windows 随驱动装在 System32，Linux 是 .so.1）
_DRIVER_LIBS = {
    "win32": ("nvcuda.dll",),
    "linux": ("libcuda.so.1", "libcuda.so"),
    "darwin": (),
}

# CUDA 13 的**算力地板**：它删掉了 sm<7.5 的离线编译（Turing = 7.5 是下限）。
# 低于它的卡（Pascal 6.x、Volta 7.0）没有可用的 SASS，只能走 CPU。
_MIN_COMPUTE_CAP = 7.5


def _driver_lib_names() -> tuple[str, ...]:
    """这台平台上要找的驱动库名（找不到就说明没有 NVIDIA 驱动）。"""
    if sys.platform.startswith("win"):
        return _DRIVER_LIBS["win32"]
    if sys.platform.startswith("linux"):
        return _DRIVER_LIBS["linux"]
    return _DRIVER_LIBS["darwin"]


def _load_gpu_lib(lib_path) -> tuple[bool, str]:
    """试着把 CUDA 版产物加载起来。返回 `(ok, 失败原因)`。

    这一步本身就是一道有用的判据：产物如果缺 VC 运行库（`MSVCP140.dll` /
    `VCRUNTIME140.dll` —— 它是**动态依赖**，不像 cudart 那样静态链进去了），
    会在这里失败，而"缺 VC 运行库"比"第一次生成时莫名其妙崩掉"具体得多。

    ⚠️ 加载成功**不代表有显卡驱动**：`nvcuda.dll` 是延迟加载的（见上面的实测）。
    """
    try:
        loader = ctypes.WinDLL if sys.platform.startswith("win") else ctypes.CDLL
        loader(str(lib_path))
    except OSError as e:
        return False, f"CUDA 版产物加载失败：{e}（缺 VC 运行库？）"
    return True, ""


def _device_compute_cap(drv, index: int) -> str:
    """查第 `index` 块卡的算力，形如 `"12.0"`。

    查不到返回 `""` —— **不算失败**（`cuDeviceComputeCapability` 在老驱动上可能
    没有；不能因为它缺席就把有卡的机器拦在门外）。
    """
    fn = getattr(drv, "cuDeviceComputeCapability", None)
    if fn is None:
        return ""
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                   ctypes.c_int]
    major, minor = ctypes.c_int(0), ctypes.c_int(0)
    if int(fn(ctypes.byref(major), ctypes.byref(minor), index)) != 0:
        return ""
    return f"{major.value}.{minor.value}"


def _driver_devices() -> tuple[int, str, str]:
    """问 CUDA 驱动要设备。返回 `(设备数, 主卡算力, 失败原因)`。

    拆出来单独一个函数是为了**能被测试猴补**：真机上的返回值取决于有没有卡，
    单元测试里没法断言（`smoke_test.py` 就是替换它来跑正/负两条路）。
    """
    names = _driver_lib_names()
    last = ""
    for name in names:
        try:
            drv = ctypes.CDLL(name)
        except OSError as e:
            last = str(e)
            continue
        init = getattr(drv, "cuInit", None)
        count = getattr(drv, "cuDeviceGetCount", None)
        if init is None or count is None:
            last = f"{name} 里没有 cuInit / cuDeviceGetCount"
            continue
        init.restype = ctypes.c_int
        init.argtypes = [ctypes.c_uint]
        count.restype = ctypes.c_int
        count.argtypes = [ctypes.POINTER(ctypes.c_int)]
        rc = int(init(0))
        if rc != 0:
            return 0, "", f"CUDA 驱动初始化失败（cuInit 返回 {rc}）"
        n = ctypes.c_int(0)
        rc = int(count(ctypes.byref(n)))
        if rc != 0:
            return 0, "", f"CUDA 驱动报不出设备（cuDeviceGetCount 返回 {rc}）"
        if n.value <= 0:
            return 0, "", "驱动没有报出任何 CUDA 设备"
        return int(n.value), _device_compute_cap(drv, 0), ""
    return 0, "", f"没找到 CUDA 驱动（{' / '.join(names)}）：{last}"


def probe_gpu() -> dict:
    """探测这台机器现在能不能真的跑 GPU 后端。

    三道门，**从便宜到贵**，全过才算可用：

    1. **CUDA 版产物在不在**（`rpg_engine_cuda.dll`）—— 不在就不可能用 GPU。
    2. **它能不能被加载** —— 缺 VC 运行库在这里就暴露（比"第一次生成时崩"具体）。
       ⚠️ 这一道**过不了不代表**没驱动：`nvcuda.dll` 是延迟加载的，
       没驱动的机器上库照样能加载（见上面那段实测）。
    3. **驱动能不能报出设备**（`cuInit` + `cuDeviceGetCount > 0`）—— 没 N 卡、
       没装驱动、驱动过旧，都在这里挂掉；顺带查主卡算力，低于 CUDA 13 的地板
       （7.5 / Turing）时给出更具体的原因。

    返回 `{available, lib, devices, compute_cap, reason}`；`reason` 是给界面看的
    人话（不可用时才非空）。
    """
    out: dict = {"available": False, "lib": "", "devices": 0,
                 "compute_cap": "", "reason": ""}

    lib = find_lib(backend="gpu")
    if lib is None or lib_backend_hint(lib) != "gpu":
        out["reason"] = "没找到 CUDA 版产物（rpg_engine_cuda）"
        return out
    out["lib"] = Path(lib).name

    ok, why = _load_gpu_lib(lib)
    if not ok:
        out["reason"] = why
        return out

    devices, cap, why = _driver_devices()
    if devices <= 0:
        out["reason"] = why or "没有可用的 CUDA 设备"
        return out
    out["devices"] = devices
    out["compute_cap"] = cap

    if cap:
        try:
            too_old = float(cap) < _MIN_COMPUTE_CAP
        except ValueError:
            too_old = False
        if too_old:
            out["reason"] = (f"显卡算力 {cap} 低于 CUDA 13 的下限 "
                             f"{_MIN_COMPUTE_CAP}（Turing）")
            return out

    out["available"] = True
    return out


_gpu_probe_cache: dict | None = None


def gpu_probe(refresh: bool = False) -> dict:
    """探测结果（**进程内缓存**）。

    用户拍板"游戏启动时自动检查一下就好"，所以默认只探一次、之后一直用缓存 ——
    每次打开设置抽屉都去 `LoadLibrary` 一遍既没必要，也会让界面有多余延迟。
    想强制重探（诊断用）传 `refresh=True`。
    """
    global _gpu_probe_cache
    if _gpu_probe_cache is None or refresh:
        _gpu_probe_cache = probe_gpu()
    return _gpu_probe_cache


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

    def model_backend(self, model: int) -> str:
        """这份权重**实际**跑在哪个后端（读 `model_info`，权威）。

        需要先加载模型（`model_info` 的入参是模型句柄）—— 所以它用在自检脚本与
        "首次生成之后"的日志里，而不是给界面画状态用（界面不为了显示一行字去搬
        1 GiB 权重上卡）。
        """
        return backend_from_info(self.model_info(model))

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


def unload_all() -> int:
    """把这个进程**已经加载的权重**全部卸掉，返回卸掉了几份。

    🔴 为什么非要有这一步（RPGBarEngineCUDA 的 `docs/DESIGN.md` §9）：引擎里模型/
    会话是**命名空间级静态对象**，它们的 `DeviceBuffer` 析构会调 `cudaFree`。拖到
    进程退出才析构的话，那时 CUDA 上下文往往**已经销毁**，于是每次退出都刷一屏：

        [rpg_engine warn] cudaFree 失败：... driver shutting down

    （Windows 侧实测：一次 `engine_probe.py` 退出刷了 100+ 行。）所以退出前主动卸
    干净 —— 本函数已经挂在 `atexit` 上，正常运行不用手动调。

    ⚠️ **不能挪到 `EngineProvider.close()` 里**：同一份权重通常被多个 Provider 共用
    （DM 与助手同进程），谁先 close 谁就会把别人的会话一起带走 —— 这是
    `EngineProvider._close_sync` 的注释里写明的事。这里按**库**遍历它登记过的模型，
    天然只卸一次、且卸载时机在所有人都不再用它之后。
    """
    n = 0
    for lib in list(_libs.values()):
        try:
            for m in lib.loaded_models():
                lib.unload_model(m)
                n += 1
        except Exception:  # noqa: BLE001  退出路径，任何异常都不该再往外冒
            pass
    return n


atexit.register(unload_all)


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
