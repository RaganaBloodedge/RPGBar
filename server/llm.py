"""LLM Provider 抽象：OpenAI 兼容接口，云 API 与本地模型共用同一套。

三条通道，对外都是同一组方法（`chat` / `chat_with_tools` / `close`）：

- 云端 API：https://api.deepseek.com/v1、https://api.openai.com/v1、…（需要 api_key）
- 本地服务：llama.cpp server（`llama-server`，默认 :8080）、Ollama（:11434/v1）、LM Studio（:1234/v1）
- **内置引擎**：进程内直连 `rpg_engine.dll`，零 HTTP（见 `engine.py`）

除了最基础的单轮 `chat()`，还提供标准 Agent 所需的 `chat_with_tools()`：
把工具（JSON Schema）发给模型，模型返回 `tool_calls`，由调用方执行后把结果回灌。
"""
import asyncio
import json
import threading
from pathlib import Path

from . import engine as engine_mod


class ModelError(Exception):
    """模型调用失败（网络、鉴权、参数等）。"""


class ToolCallingUnsupported(ModelError):
    """该模型/服务不支持工具调用——调用方应降级为单轮 JSON 协议。"""


class LLMProvider:
    name = "base"
    supports_tools = True

    async def chat(self, messages, json_mode: bool = False) -> str:
        raise NotImplementedError

    async def chat_with_tools(self, messages, tools) -> dict:
        """返回 {"content": str, "tool_calls": [...]}。"""
        raise NotImplementedError

    async def close(self):
        pass


def _looks_like_no_tools(status: int, body: str) -> bool:
    if status not in (400, 404, 422, 500):
        return False
    low = (body or "").lower()
    return any(k in low for k in ("tool", "function", "tool_choice", "tools"))


class OpenAICompatProvider(LLMProvider):
    name = "openai-compat"

    def __init__(self, base_url, api_key, model, temperature=0.8, timeout=120.0, label="cloud"):
        import httpx

        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.temperature = temperature
        self.label = label  # "cloud" / "local"
        self.name = f"openai-compat[{label}]"
        self._httpx = httpx
        self._client = httpx.AsyncClient(timeout=timeout)

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def chat(self, messages, json_mode: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = await self._client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
        )
        if resp.status_code >= 400:
            raise ModelError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return (data["choices"][0]["message"].get("content") or "")

    async def chat_with_tools(self, messages, tools) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
        resp = await self._client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
        )
        if resp.status_code >= 400:
            body = resp.text or ""
            if tools and _looks_like_no_tools(resp.status_code, body):
                raise ToolCallingUnsupported(f"HTTP {resp.status_code}: {body[:200]}")
            raise ModelError(f"HTTP {resp.status_code}: {body[:200]}")
        data = resp.json()
        msg = data["choices"][0]["message"]
        return {"content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}

    async def close(self):
        await self._client.aclose()


def _to_openai_calls(calls) -> list:
    """把引擎的 `{name, arguments}` 转成 OpenAI 的 `{id, type, function:{…}}`。

    转这一层是必须的，不是风格问题：`agents/base.py` 的工具循环按 OpenAI 形状取
    `call["function"]["name"]` 与 `fn["arguments"]`（字符串）。让 Agent 层去兼容
    两种形状，等于把"换个后端"的成本摊到每个消费点上。

    `arguments` 为 null 的调用**直接丢掉**：那表示引擎没能解析出合法参数（引擎自己
    已经记了带原文的日志）。放一个 `{}` 出去会被当成"参数为空的调用"真的执行一次，
    比丢掉更糟。
    """
    out = []
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        args = c.get("arguments")
        if not isinstance(args, dict):
            continue
        out.append(
            {
                "id": f"call_{len(out)}",
                "type": "function",
                "function": {
                    "name": c.get("name") or "",
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )
    return out


class EngineProvider(LLMProvider):
    """内置自研引擎（进程内原生库，零 HTTP）。

    **两个后端、同一个 ABI**：`backend="cpu"` 用 `RPGBarEngine`（纯 CPU），
    `backend="gpu"` 用 `RPGBarEngineCUDA`（CUDA）—— 宿主侧的差别只有"加载哪一个
    文件"，调用代码一行不改（这正是"一个 ABI、两个实现"当初要买的东西）。
    请求的后端与实际加载的后端可能不同（gpu 缺库时退回 cpu），所以两个值都记下来：
    `self.backend` 是配置、`self.lib_backend` 是事实。

    **声明支持工具调用。** 这一点与"小模型多半不会函数调用"的常见印象相反，但在
    这条路径上是事实：Qwen3 是原生 tool calling 模型，引擎的 ChatML 渲染与
    `<tool_call>` 解析都对着官方模板逐字节对过（RPGBarEngine 的 `docs/DESIGN.md` §16）。
    让 DM 在这条路径上降级成单轮 JSON，等于白丢模型本来就会的能力。

    `temperature` 与 `seed` 决定"这一轮有多活"，两者是**正交的两条链**：

    - `temperature` 是引擎内的**确定性筛选**（缩放 logits，改变分布形状），每轮随
      请求传，所以在设置里改完立即生效；
    - `seed` 是随机数的来源，房间创建时掷一次、整局固定（`main.Room.seed`），
      引擎按 `u = f(seed, KV 绝对位置)` 展开。

    同一个 `u` 配不同温度会选出不同 token；温度趋 0 时分布变成尖刺，那时无论种子
    是什么都选同一个 token（贪心）。反过来，**不给 seed 就等于没有变化** —— 引擎
    默认 0，每个房间每一局都会抽出同一串随机数。
    """

    name = "engine"
    supports_tools = True

    def __init__(self, lib, model_path, *, threads=0, context=4096, max_tokens=512,
                 temperature=0.8, seed=None, stop=None, think=True, label="engine",
                 backend="cpu"):
        self.lib = lib
        self.backend = engine_mod.normalize_backend(backend)   # 请求的后端（cpu / gpu）
        # **实际**用的是哪个后端（按文件名推断，只是提示：权威来源是加载后读
        # model_info —— 见 _log_backend_once）。请求 gpu 而没找到 CUDA 版产物时
        # 这两个值不一样，界面据此标出"实际：CPU"。
        self.lib_backend = engine_mod.lib_backend_hint(getattr(lib, "path", ""))
        self.model_path = str(model_path)
        self.threads = int(threads)
        self.context = int(context)
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self.seed = None if seed is None else int(seed)
        self.stop = [str(s) for s in (stop or [])]
        self.think = bool(think)
        self.label = label
        self.name = f"engine[{Path(self.model_path).stem}]"
        self.last_usage: dict = {}
        self.actual_backend = ""   # 首次加载后由 model_info 填（权威）

        self._model = None
        self._session = None
        self._init_lock = threading.Lock()
        # 引擎在同一份权重上**不支持并发 generate**（两个房间同时跑 → 进程无声消失）。
        # 锁按**权重路径**共享（`engine.model_lock`），而不是每个 Provider 一把 ——
        # 一个房间一个 Provider，实例级锁在多房间并发时形同虚设。
        self._gen_lock = engine_mod.model_lock(self.model_path)

    # ---- 生命周期 ----

    def _log_backend_once(self) -> None:
        """首次加载权重后，把"**实际**跑在哪个后端"打出来（一次）。

        为什么值得一行日志：请求的后端与实际用的后端**可能不一样** —— 选了 GPU
        但机器上没有 CUDA 版产物时 `find_lib` 会退回 CPU 版（见 engine.find_lib）。
        玩家反馈"我明明选了 GPU 怎么还这么慢"时，这行日志与界面上的"实际：CPU"
        是同一个答案。`model_info` 需要模型句柄，所以只能在这里做（不为了显示一行
        字去搬 1 GiB 权重上卡）。

        这一整段是**日志**，不是功能：任何异常都不许冒出去（测试里的假 lib、
        老引擎没有这个方法……都不该让一次生成失败）。
        """
        if self.actual_backend:
            return
        try:
            self.actual_backend = self.lib.model_backend(self._model)
            name = Path(getattr(self.lib, "path", "") or "").name or "?"
            note = ""
            if (self.backend == "gpu" and self.actual_backend
                    and self.actual_backend != "gpu"):
                note = ("（**选的是 GPU，实际不是** —— 没找到 CUDA 版产物？"
                        "跑 python scripts/sync_engine.py --backend gpu 看看）")
            print(f"[RPGBar] 内置引擎：请求后端 {self.backend} · "
                  f"实际后端 {self.actual_backend or '未知'} · 库 {name}{note}")
        except Exception:  # noqa: BLE001  日志失败不算失败
            pass

    def _ensure(self) -> int:
        """懒建 model + session，返回会话句柄。

        懒建而不是在 `__init__` 里建：`build_provider()` 在**建房、玩家加入、改配置**
        等好几条路径上都会被调用一次，而加载权重要读 1~2.5 GB、每个会话的 KV Cache
        又是几百 MB。构造时就分配的话，一次误调用就会真吃掉这些内存。
        （同一个 Agent 的 Provider 由 `Room` 缓存，所以正常路径下只会建一次。）
        """
        with self._init_lock:
            if self._session is None:
                self._model = self.lib.model_for(
                    self.model_path, threads=self.threads, max_pos=self.context
                )
                self._log_backend_once()
                self._session = self.lib.new_session(
                    self._model, max_seq=self.context, max_tokens=self.max_tokens,
                    stop=self.stop, seed=self.seed,
                )
            return self._session

    def _close_sync(self) -> None:
        """销毁会话。**不卸载权重** —— 同一份权重通常被多个 Provider 共用（DM 与
        小助手同进程），谁先 close 谁就会把别人的会话一起带走。权重的卸载交给
        `engine.unload_all()`（已挂在 `atexit` 上）：它按**库**遍历登记过的模型，
        只在"所有人都不再需要"之后才动手，也因此不会在退出时刷一屏
        `cudaFree 失败：driver shutting down`。

        销毁也要在生成锁里：`session_destroy` 与另一个会话正在跑的 `generate` 并发
        同样会让引擎崩。
        """
        with self._gen_lock:
            with self._init_lock:
                if self._session is not None:
                    try:
                        self.lib.destroy_session(self._session)
                    finally:
                        self._session = None

    async def close(self):
        self._close_sync()

    def __del__(self):
        try:
            self._close_sync()
        except Exception:  # noqa: BLE001  解释器收尾时什么都可能缺，静默即可
            pass

    # ---- 生成 ----

    def _run(self, messages, tools) -> dict:
        """同步跑一轮（阻塞几十秒，调用方负责丢到线程里）。

        **建会话与前向必须在同一把锁里**：`session_create` 与另一个会话正在跑的
        `generate` 并发时，引擎会崩 —— 实测两个房间同时点「助手建议」，日志停在
        「已创建会话 #2」然后整个进程无声消失。所以锁的范围是「取会话 → 前向」整段，
        而不是只包 `generate`。
        """
        req = {
            "messages": [dict(m) for m in messages],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "enable_thinking": self.think,
        }
        if tools:
            req["tools"] = tools
        with self._gen_lock:
            session = self._ensure()
            reply = self.lib.generate(session, req)
        self.last_usage = reply.get("usage") or {}
        return reply

    async def chat(self, messages, json_mode: bool = False) -> str:
        # json_mode 对引擎没有意义：它的输出格式靠 messages 里的格式提示约束
        # （`agents/` 的 json_hint 就是干这个的）。这里收下参数只为接口一致。
        reply = await asyncio.to_thread(self._run, messages, None)
        return reply.get("text") or ""

    async def chat_with_tools(self, messages, tools) -> dict:
        reply = await asyncio.to_thread(self._run, messages, tools)
        return {
            "content": reply.get("text") or "",
            "tool_calls": _to_openai_calls(reply.get("tool_calls")),
        }

    # ---- 界面用的状态 ----

    def describe(self) -> dict:
        return {
            "model": Path(self.model_path).name,
            "context": self.context,
            "max_tokens": self.max_tokens,
            "think": self.think,
            "backend": self.backend,          # 请求的后端（cpu / gpu）
            "lib_backend": self.lib_backend,  # 按文件名推断的实际后端（提示）
            "usage": dict(self.last_usage),
        }


def build_provider(slot, *, seed: int | None = None) -> LLMProvider | None:
    """由一个「模型槽位」配置构建 Provider；返回 None 表示走脚本化兜底。

    槽位字段：kind + 各自的连接参数。
    - kind=off（默认，即"留空"）：不接模型，全部走脚本化兜底。
    - kind=cloud：必须有 api_key。
    - kind=local：不要求 api_key（本地服务一般不需要）。
    - kind=engine：内置引擎，需要 `lib`（动态库）与 `model`（权重文件），二者在
      `config.load_config()` 里已按 `engine` 段填好；缺一个就返回 None（静默退化，
      与"引擎加载失败 → 视为 off"是同一条降级链）。
      `backend`（cpu / gpu）决定找**哪一份**库：gpu 先找 CUDA 版产物
      （`rpg_engine_cuda.dll`），找不到退回 CPU 版（不返回 None —— 那样 DM 会变成
      脚本化，玩家看到的是"游戏坏了"，见 `engine.find_lib`）。

    `seed` 是**运行时状态**而非配置，所以走形参而不是槽位字段 —— 槽位会原样下发
    给前端，种子只该进服务端日志。目前只有 `kind=engine` 用它（引擎是纯函数，
    不给种子则每局随机流相同）；云 / 本地服务的温度由对方服务端抽样，暂不转发。
    """
    if not isinstance(slot, dict):
        return None
    kind = (slot.get("kind") or "off").lower()
    if kind in ("off", "none", "scripted", ""):
        return None

    if kind == "engine":
        backend = engine_mod.normalize_backend(slot.get("backend"))
        lib_path = engine_mod.find_lib((slot.get("lib") or "").strip(), backend)
        model_path = engine_mod.find_model((slot.get("model") or "").strip())
        if lib_path is None or model_path is None:
            return None
        try:
            lib = engine_mod.open_lib(lib_path)
        except engine_mod.EngineError:
            # 库坏了/版本不对 → 当作没接模型。这条路径上"能玩"比"报错"重要。
            return None
        return EngineProvider(
            lib,
            model_path,
            threads=int(slot.get("threads") or 0),
            context=int(slot.get("context") or 4096),
            max_tokens=int(slot.get("max_tokens") or 512),
            temperature=float(slot.get("temperature", 0.8)),
            seed=seed,
            stop=slot.get("stop"),
            think=bool(slot.get("think", False)),
            label="engine",
            backend=backend,
        )

    base_url = (slot.get("base_url") or "").strip()
    model = (slot.get("model") or "").strip()
    api_key = (slot.get("api_key") or "").strip()
    if kind == "cloud" and not api_key:
        return None
    if not base_url or not model:
        return None
    return OpenAICompatProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=float(slot.get("temperature", 0.8)),
        timeout=float(slot.get("timeout", 120.0)),
        label="local" if kind == "local" else "cloud",
    )

