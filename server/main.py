"""FastAPI + WebSocket 多人房间服务。

协议（JSON 文本帧）：
  客户端 → 服务端：
    {"type":"join","name":"...","character":{...},"room":"ABC12"(可空),"script":"...(可空，建房时选)"}
    {"type":"action","text":"..."}
    {"type":"suggest"}
    {"type":"roll"}
    {"type":"configure","dm":{...}(可空),"advisor":{...}(可空),"assistant":{...}(可空)}
       # 运行期接模型；dm / assistant 仅房主可改，advisor 每人管自己的
       槽位字段：kind(off|cloud|local) + base_url + model + temperature + api_key(省略=保持原值)
  服务端 → 客户端：
    {"type":"welcome","room":"...","you":"...","late":bool,"script":"剧本名","agents":{...},
      "script_info":{...},"chunks":[...],"npcs":[...],"memory":{...},
      "prompt":{"advisor":"...","dm":"...(仅房主)"}}  # 连入即同步（含本房间已有 NPC / 档案状态）
    {"type":"agent_status","agents":{...},"changed":["dm","advisor","assistant"]}   # 响应 configure
    {"type":"system","text":"..."}
    {"type":"thinking","active":bool,"who":"dm|advisor","label":"..."}
      # 模型推理中（active=true）/ 结束（false）。引擎在 CPU 上跑一轮要几十秒到
      # 几分钟，界面靠它显示「DM 正在推理剧情…」，否则玩家只看到一片静止
    {"type":"busy","who":"dm|advisor","message":"..."}
      # 上一条请求还没跑完，本次被拒（之前是静默丢弃，玩家点了没反应、以为卡死）
    {"type":"agent_note","who":"dm|advisor","source":"llm|failed",
      "stopped":"final|error|no_provider|no_tools|max_steps","error":"...","tools":[...]}
      # 本轮到底有没有产出，只有两种：llm（真跑到模型了）/ failed（这一轮没有产出，
      # 原因在 error 里）。2026-10-13 用户拍板取消脚本化兜底后，不再"用剧本拼一段
      # 旁白冒充模型"，所以 source != llm 时界面必须把失败明说出来。
    {"type":"narration","author":"DM","text":"..."}
    {"type":"dice","player":"...","skill":"...","dc":n,"roll":n,"success":bool|null,"flag":str|null}
    {"type":"suggestions","options":[...],"agent":{"source":"llm|failed","tools":[...]}}
    {"type":"recap","recap":{"scene_path":[...],"flags":[...],"recent":[...],...}}  # 仅发给中途加入者
    {"type":"state","state":{...}}   # state.flag_details=[{id,label}]；state.npcs=[人物卡摘要]；state.memory=档案状态
    {"type":"error","message":"..."}

HTTP 接口：
    GET  /api/version          版本 / 当前剧本 / 三个 Agent 的接线状态 / 剧情档案与嵌入模型状态
    GET  /api/net              本机可被访问的地址（含虚拟网卡，供主机分享给朋友）
    POST /api/models/test      探测一个模型槽位（含工具调用能力）
    GET  /api/scripts          列出可用的剧本文件
    POST /api/scripts/inspect  读取某个剧本 → 自动切片 → 返回次级 prompt（只看不改）
    POST /api/scripts/load     读取并切换「活动剧本」（新开的房间用它）
"""
import asyncio
import json
import os
import re
import secrets
import string
import time
from pathlib import Path

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agents import (
    AdvisorAgent,
    AdvisorContext,
    AssistantAgent,
    NarratorAgent,
    NarratorContext,
    SummaryCtx,
    describe_agents,
)
from .chronicle import Chronicle
from .config import RESOURCE_DIR, USER_DIR, load_config
from .dice import roll
from .dm import DM
from .embedding import get_embedder
from .engine import BACKENDS, derive_seed, gpu_probe, normalize_backend
from .llm import ToolCallingUnsupported, build_provider
from .npc import HELP_VERBS, HURT_VERBS, NpcService
from .rag import ScriptStore
from .state_machine import GameState

cfg = load_config()


def _server_port() -> int:
    """服务器端口（环境变量 PORT 优先，与 main() 保持一致）。"""
    return int(os.environ.get("PORT", cfg["server"]["port"]))


def _resolve_script(rel: str) -> Path:
    """解析剧本路径：相对路径按只读资源目录解析；不存在则回退到内置样例剧本。"""
    p = Path(rel)
    path = p if p.is_absolute() else (RESOURCE_DIR / p)
    if path.exists():
        return path
    fallback = RESOURCE_DIR / "scripts" / "sample_script.json"
    print(f"[RPGBar] 剧本「{rel}」不存在，已回退到 {fallback.name}")
    return fallback


def script_dirs() -> list:
    """可读剧本的目录：只读资源目录 + 可写用户目录（打包后 exe 旁边）。"""
    out, seen = [], set()
    for d in (RESOURCE_DIR / "scripts", USER_DIR / "scripts"):
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


SCRIPT_SUFFIX = (".json", ".md", ".txt", ".markdown")


def _rel_of(path: Path) -> str:
    """把绝对路径表示成「相对资源目录」或「相对用户目录」的短形式。"""
    for d in script_dirs():
        try:
            return path.relative_to(d).as_posix()
        except ValueError:
            continue
    return str(path)


def _safe_script_path(rel: str):
    """只允许读取 scripts/ 目录内的文件。

    服务端默认绑 0.0.0.0（同局域网的玩家都能访问），因此**不能**支持任意绝对路径，
    否则等于给同网段的人开了个任意文件读取接口。想换剧本就把文件放进 scripts/ 目录。
    """
    if not isinstance(rel, str) or not rel.strip() or rel != rel.strip():
        return None
    parts = re.split(r"[\\/]+", rel)
    if any(p in ("..", "", ".") for p in parts):
        return None
    for d in script_dirs():
        p = (d / rel).resolve()
        try:
            p.relative_to(d.resolve())
        except ValueError:
            continue
        if p.is_file() and p.suffix.lower() in SCRIPT_SUFFIX:
            return p
    return None


def _script_entry(path: Path) -> dict:
    """列出一个剧本文件的概要（顺便把它读一遍做自检）。"""
    rel = _rel_of(path)
    try:
        store = ScriptStore(path)
    except Exception as e:  # noqa: BLE001
        return {"path": rel, "file": path.name, "title": path.stem, "error": f"{type(e).__name__}: {e}"}
    return {
        "path": rel,
        "file": path.name,
        "title": store.title,
        "fmt": store.fmt,
        "structured": store.doc.structured,
        "scenes": len(store.scenes),
        "flags": len(store.flag_ids),
        "chunks": len(store.chunk_list),
        "warnings": store.warnings,
    }


def list_scripts() -> list:
    seen, items = set(), []
    for d in script_dirs():
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in SCRIPT_SUFFIX and p.name not in seen:
                seen.add(p.name)
                items.append(_script_entry(p))
    return items


# ---- 活动剧本（room 创建时取用；切换只影响新开的房间）----
active = {"path": _resolve_script(cfg.get("script") or "scripts/sample_script.json")}
active["rel"] = _rel_of(active["path"])
store = ScriptStore(active["path"], label=active["rel"])

default_dm_slot = dict(cfg["models"]["dm"])
default_advisor_slot = dict(cfg["models"]["advisor"])
default_assistant_slot = dict(cfg["models"].get("assistant") or {})
# 内置引擎段（config.json 的 engine）—— 只把**可公开调节**的几项开放给界面，
# lib / model 这类本机路径永远只留在服务端（与槽位的 api_key 同理）。
default_engine_slot = dict(cfg.get("engine") or {})
NPC_AUTO_PERSONA = bool((cfg.get("npc") or {}).get("auto_persona", True))
# 剧情档案：只增 JSONL + 混合检索（BM25 + 向量）。SUMMARY_EVERY 条新记录滚动摘要一次。
MEM_CFG = cfg.get("memory") or {}
CHRONICLE_ENABLED = bool(MEM_CFG.get("chronicle", True))
SUMMARY_EVERY = max(2, int(MEM_CFG.get("summary_every", 8)))
DATA_DIR = USER_DIR / "data"
EMBEDDER = get_embedder() if CHRONICLE_ENABLED else None

print(f"[RPGBar] 剧本：{store.title}（{len(store.scenes)} 场景 / {len(store.flag_ids)} 个 flag / {len(store.chunk_list)} 个切片）")
for w in store.warnings:
    print(f"[RPGBar] 剧本提示：{w}")
if CHRONICLE_ENABLED:
    _es = EMBEDDER.status() if EMBEDDER is not None else {"available": False, "reason": "未启用"}
    if _es["available"]:
        print(f"[RPGBar] 剧情档案：已启用（{_es['model']} {_es['dim']} 维 + BM25 混合检索）")
    else:
        print(f"[RPGBar] 剧情档案：已启用（纯 BM25；未加载嵌入模型：{_es['reason']}）")
def backend_text(eng) -> str:
    """把内置引擎的后端状态说成一句人话（启动日志与错误文案用）。

    界面有自己的文案（`web/app.js`）。这里的存在理由是**看日志的人**：选了 GPU
    却落回 CPU 时，日志必须说出来（退回规则见 `engine.find_lib`）—— 否则"怎么
    还是这么慢"会变成一场没有线索的排查。
    """
    eng = eng or {}
    req = normalize_backend(eng.get("backend"))
    lib_be = str(eng.get("lib_backend") or "")
    if req == "gpu" and lib_be == "cpu":
        return "GPU 后端（没找到 rpg_engine_cuda，实际退回了 CPU）"
    if not lib_be:
        return "GPU 后端" if req == "gpu" else "CPU 后端"
    return "GPU 后端（CUDA）" if req == "gpu" else "CPU 后端"


_agents = describe_agents(default_dm_slot, default_advisor_slot, default_assistant_slot)
for key in ("dm", "advisor", "assistant"):
    a = _agents.get(key)
    if not a:
        continue
    if a["ready"]:
        if a["kind"] == "engine":
            where = "进程内直连 · " + backend_text(a.get("engine"))
        else:
            where = a["base_url"]
        print(f"[RPGBar] {a['label']}：{a['kind']} · {where} · {a['model']}")
    else:
        print(f"[RPGBar] {a['label']}：未接模型（留空）—— 该 Agent 不产出内容")
print("[RPGBar] 也可以在游戏内的「模型设置」里随时接入（云 API 或本地模型）。")

# GPU 可用性**在启动时探一次**（用户拍板；结果进进程内缓存，界面复用同一份）。
# 显式打出来是因为"没有独显的机器上 GPU 选项是灰的"这件事必须**有据可查** ——
# 否则下一句就是"我明明有卡，怎么灰了"（判据见 engine.probe_gpu）。
_gpu = gpu_probe()
if _gpu["available"]:
    # 算力可能查不到（老驱动没有 cuDeviceComputeCapability）—— 那就只说设备数，
    # 不编一个"未知"出来（查不到 ≠ 有问题）。
    _cap = f" · 算力 {_gpu['compute_cap']}" if _gpu.get("compute_cap") else ""
    print(f"[RPGBar] GPU 后端可用：{_gpu['devices']} 个 CUDA 设备{_cap} · {_gpu['lib']}")
else:
    print(f"[RPGBar] GPU 后端不可用（{_gpu['reason']}）"
          "—— 界面上的「GPU」选项会置灰，只能选 CPU。")


# ---- 模型槽位的清洗与合并 ----
ALLOWED_KINDS = ("off", "cloud", "local", "engine")


def clean_slot_patch(patch) -> dict:
    """只接受白名单字段，防止脏数据进服务端。"""
    if not isinstance(patch, dict):
        return {}
    out = {}
    kind = patch.get("kind")
    if isinstance(kind, str) and kind.lower() in ALLOWED_KINDS:
        out["kind"] = kind.lower()
    for key in ("base_url", "model"):
        if isinstance(patch.get(key), str):
            out[key] = patch[key].strip()
    if "api_key" in patch and isinstance(patch["api_key"], str):
        out["api_key"] = patch["api_key"].strip()  # 传空串 = 清除
    if "temperature" in patch:
        try:
            t = float(patch["temperature"])
            out["temperature"] = max(0.0, min(2.0, t))
        except (TypeError, ValueError):
            pass
    if out.get("base_url") and not out["base_url"].startswith(("http://", "https://")):
        out.pop("base_url")
    return out


def merge_slot(slot: dict, patch: dict) -> dict:
    """把补丁合并进槽位；api_key 未出现则保持原值。"""
    for k, v in patch.items():
        slot[k] = v
    if (slot.get("kind") or "off") == "off":
        # 关闭时不要求 base_url/model 合法
        return slot
    return slot


def clean_engine_patch(patch) -> dict:
    """内置引擎段的白名单清洗（只收界面能调的那几项）。

    `lib` / `model`（本机路径）**不在白名单里** —— 它们是服务端的事，收进来就等于
    让任意客户端去指一个能被加载的 dll，且没有意义（一个进程只加载一份库）。

    `backend` 在白名单里，但**只收 `cpu` / `gpu` 两个值**（`engine.BACKENDS`）：
    它做的是"在这张枚举好的产物名单里挑一个"（`rpg_engine.dll` / `rpg_engine_cuda.dll`），
    与上面那条边界不冲突 —— 客户端挑的是后端，指不了文件。
    """
    if not isinstance(patch, dict):
        return {}
    out = {}
    for key in ("enabled", "think"):
        if key in patch:
            out[key] = bool(patch[key])
    if "backend" in patch:
        b = normalize_backend(patch["backend"], default="")
        if b in BACKENDS:
            out["backend"] = b
    for key, lo, hi in (("context", 256, 32768), ("max_tokens", 16, 4096)):
        if key in patch:
            try:
                out[key] = max(lo, min(hi, int(patch[key])))
            except (TypeError, ValueError):
                pass
    return out


def _polarity(text: str) -> float:
    """粗略判断一次行动对 NPC 是好是坏（只用来微调好感度，不做剧情判定）。"""
    t = text or ""
    hurt = sum(1 for v in HURT_VERBS if v in t)
    help_ = sum(1 for v in HELP_VERBS if v in t)
    if hurt and not help_:
        return -0.25
    if help_ and not hurt:
        return 0.15
    return 0.0


class Room:
    def __init__(self, code, script_store=None):
        self.code = code
        # 房间在创建时「钉住」当时的活动剧本：之后服务端换剧本，进行中的对局不受影响。
        self.store = script_store or store
        # 本局随机种子：建房时掷一次、整局固定（引擎是纯函数，不给种子则每个房间
        # 每一局的随机流都一模一样 —— "换个房间重开"其实拿到同一串随机数）。
        # 只进服务端日志、不下发：想复现某一局的剧情，从日志里取这个数。
        self.seed = secrets.randbits(63)
        self.state = GameState(self.store, self.store.start_scene)
        self.clients = []  # list[(name, websocket)]
        self.lock = asyncio.Lock()
        self.owner = None  # 房主（第一个加入的人）才能改 DM / 主机小助手模型
        self.dm_slot = dict(default_dm_slot)
        self.assistant_slot = dict(default_assistant_slot)
        # 内置引擎的「界面可调项」（enabled / think / context / max_tokens）。
        # 房间级：一个房间改了不影响别的房间；lib / model 仍取服务端全局配置。
        self.engine_slot = dict(default_engine_slot)
        self.advisors = {}  # name -> {"slot": dict, "agent": AdvisorAgent}
        self._narrator = None
        self._assistant = None
        # NPC 子系统：人物卡 + 隔离记忆（房间级，随房间钉住剧本）
        self.npc_enabled = NPC_AUTO_PERSONA
        self.npc = NpcService(self.store) if self.npc_enabled else None
        # 剧情档案：本局已输出的主线全文（只增 JSONL）+ 混合检索索引
        self.chronicle = (
            Chronicle(DATA_DIR / f"chronicle-{code}.jsonl", embedder=EMBEDDER, title=self.store.title)
            if CHRONICLE_ENABLED
            else None
        )
        self._summarized = len(self.chronicle) if self.chronicle else 0

    # ---- Agent 生命周期 ----
    def _effective_slot(self, slot):
        """把「内置引擎」小节的设置并入 engine 槽位。

        并入**只发生在内存里的一次拷贝上**：槽位本身不动（它还要原样描述给前端），
        引擎段里的本机路径也不会因此流出去。引擎被界面关掉时返回 None —— 调用方
        按「这个槽位没有模型」处理（然后就显式失败，没有脚本化兜底）。
        """
        if (slot.get("kind") or "off").lower() != "engine":
            return slot
        merged = dict(slot)
        for key in ("enabled", "think", "context", "max_tokens", "backend"):
            if key in self.engine_slot:
                merged[key] = self.engine_slot[key]
        if not merged.get("enabled"):
            return None
        return merged

    def _provider(self, slot, label):
        """按会话派生种子后构建 Provider。

        每个会话（DM / 主机小助手 / 每个玩家的小助手）拿一个**独立**的种子：
        大家都要有创造性，但不该共用同一条随机流。种子落在会话上而不是槽位上 ——
        槽位是配置、会原样下发给前端。
        """
        slot = self._effective_slot(slot)
        if slot is None:
            return None
        return build_provider(slot, seed=derive_seed(self.seed, label))

    def narrator(self) -> NarratorAgent:
        if self._narrator is None:
            self._narrator = NarratorAgent(
                self.store, self._provider(self.dm_slot, "dm"),
                npc=self.npc, chronicle=self.chronicle
            )
        return self._narrator

    def assistant(self) -> AssistantAgent:
        """主机端小模型（NPC 人设 / 反应权重 / 摘要）。"""
        if self._assistant is None:
            agent = AssistantAgent(self.store, self._provider(self.assistant_slot, "assistant"))
            self._assistant = agent
            if self.npc is not None:
                self.npc.set_agent(agent)
        return self._assistant

    def advisor(self, name) -> AdvisorAgent:
        info = self.advisors.get(name)
        if info is None or info.get("agent") is None:
            slot = info["slot"] if info else dict(default_advisor_slot)
            agent = AdvisorAgent(self.store, self._provider(slot, f"advisor:{name}"))
            info = {"slot": slot, "agent": agent}
            self.advisors[name] = info
        return info["agent"]

    def agents_status(self, name=None) -> dict:
        status = describe_agents(
            self._effective_slot(self.dm_slot),
            self._effective_slot(self.advisor_slot_for(name)),
            self._effective_slot(self.assistant_slot),
        )
        status["owner"] = self.owner
        status["engine"] = self.engine_public(status)
        return status

    def engine_public(self, status=None) -> dict:
        """内置引擎的「界面可见状态」（绝不含权重路径等本机信息）。

        `ready` 取自 DM 槽位的引擎探测结果 —— 与「模型设置」里显示的是同一份
        判据，免得两个地方各说各话。

        后端那三个字段也来自同一份探测：`backend`（请求的后端）、`lib_backend`
        （实际会加载的那份库）、`backend_fallback`（请求 GPU 却落回 CPU）。
        DM 槽位不是 engine 时探测结果为空 —— 那就回落到房间配置里的请求值，
        界面至少要能正确显示"当前选的是哪个"。
        """
        if status is None:
            status = describe_agents(self._effective_slot(self.dm_slot), None, None)
        detail = (status.get("dm") or {}).get("engine") or {}
        eng = self.engine_slot
        req_backend = normalize_backend(
            detail.get("backend") or eng.get("backend")
        )
        gpu = gpu_probe()
        return {
            "enabled": bool(eng.get("enabled")),
            "think": bool(eng.get("think")),
            "context": int(eng.get("context") or 0),
            "max_tokens": int(eng.get("max_tokens") or 0),
            "ready": bool(detail.get("ready")),
            "model": detail.get("model_name", ""),
            "backend": req_backend,
            "lib_backend": str(detail.get("lib_backend") or ""),
            "backend_fallback": bool(detail.get("backend_fallback")),
            "lib_name": str(detail.get("lib_name") or ""),
            # GPU 可用性：**这台机器**能不能真的跑 GPU 后端（探测见
            # engine.probe_gpu）。界面据此把「GPU」那一项置灰 —— CUDA 版产物
            # 随包分发，所以"文件在"不代表"能用"。
            "gpu_available": bool(gpu.get("available")),
            "gpu_reason": str(gpu.get("reason") or ""),
            "gpu_devices": int(gpu.get("devices") or 0),
            "gpu_compute_cap": str(gpu.get("compute_cap") or ""),
        }

    def advisor_slot_for(self, name):
        info = self.advisors.get(name)
        return info["slot"] if info else dict(default_advisor_slot)

    # ---- 剧本与提示词同步（建房时钉住，连入即同步）----
    def script_info(self) -> dict:
        """本房间所用剧本的概要（建房时钉住；之后换「活动剧本」不影响进行中的房间）。"""
        return {
            "title": self.store.title,
            "path": self.store.source,
            "fmt": self.store.fmt,
            "structured": bool(self.store.doc.structured),
            "scenes": len(self.store.scenes),
            "flags": len(self.store.flag_ids),
            "chunks": len(self.store.chunk_list),
            "warnings": list(self.store.warnings),
        }

    def room_context(self, name) -> dict:
        """连入房间时下发的「剧本 + Agent 次级 prompt」同步包。

        剧本与两份 prompt 都以**房间**为准，朋友连进来看到的剧本与提示词跟主机完全一致。
        DM 的次级 prompt 只发给房主（主机自己跑 DM）；其余玩家只需要小助手的**公开版**
        （他们各自在本机跑小助手，拿到的就是这份不含内幕/索引的次级 prompt）。
        """
        prompts = {"advisor": self.store.script_prompt_public, "dm": None}
        if name == self.owner:
            prompts["dm"] = self.store.script_prompt
        return {
            "script_info": self.script_info(),
            "chunks": [
                {"id": c.id, "title": c.title, "kind": c.kind, "chars": c.chars}
                for c in self.store.chunk_list
            ],
            "prompt": prompts,
        }

    async def broadcast(self, msg):
        for _, ws in list(self.clients):
            try:
                await ws.send_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                pass

    @staticmethod
    async def send_to(ws, msg):
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def send_state(self):
        snap = self.state.snapshot()
        if self.npc is not None:
            snap["npcs"] = self.npc.snapshot()
        mem = self.memory_status()
        if mem:
            snap["memory"] = mem
        await self.broadcast({"type": "state", "state": snap})

    def memory_status(self) -> dict:
        """给界面看的剧情档案状态（DM 拿的是更完整的检索能力，这里只要一个计数）。"""
        if self.chronicle is None:
            return {}
        return {
            "entries": len(self.chronicle),
            "actors": len(self.chronicle.actors()),
            "has_summary": bool(self.chronicle.summary),
            "embedding": bool(getattr(EMBEDDER, "available", False)),
        }

    async def on_join(self, name, character, ws):
        async with self.lock:
            # 加入前已开局 → 视为「中途加入」，需要 DM 补剧情
            late = self.state.is_in_progress()
            self.state.add_player(name, character)
            self.clients.append((name, ws))
            self.advisors.setdefault(name, {"slot": dict(default_advisor_slot), "agent": None})
            if self.owner is None:
                self.owner = name
            await self.send_to(
                ws,
                {
                    "type": "welcome",
                    "room": self.code,
                    "you": name,
                    "late": late,
                    "script": self.store.title,
                    "agents": self.agents_status(name),
                    # 连入即同步：本房间的剧本 + 两份 Agent 次级 prompt（DM 那份仅房主可见）
                    **self.room_context(name),
                    # 本房间已有的 NPC（人物卡）——晚加入也能认识在场的人
                    "npcs": self.npc.snapshot() if self.npc is not None else [],
                    # 本房间的剧情档案状态（已记录多少条 / 是否已有滚动摘要）
                    "memory": self.memory_status(),
                },
            )
            if not late:
                await self.broadcast({"type": "system", "text": f"{name} 加入了队伍"})
                opening = self.state.current().get("public_text", "")
                await self.broadcast({"type": "narration", "author": "DM", "text": opening})
                self._chronicle_add(opening, kind="narration")
            else:
                await self.broadcast({"type": "system", "text": f"{name} 中途加入了队伍"})
                await self.broadcast(
                    {"type": "thinking", "active": True, "who": "dm",
                     "label": "DM 正在为你补上之前的剧情…"}
                )
                try:
                    intro = await DM(self.store, self.narrator().provider).introduce(self.state, name, character)
                finally:
                    await self.broadcast({"type": "thinking", "active": False, "who": "dm"})
                if intro and intro.strip():
                    await self.broadcast({"type": "narration", "author": "DM", "text": intro})
                    self._chronicle_add(intro, kind="narration")
                else:
                    # 取消脚本化兜底（2026-10-13）：没有模型就不再编一段「新玩家登场」
                    # 的旁白，直接告诉界面这一轮没产出。
                    await self.broadcast({
                        "type": "agent_note", "who": "dm", "source": "failed",
                        "stopped": "no_provider",
                        "error": "没有可用的模型，生成不了带你入场的旁白",
                        "tools": [],
                    })
                # 只发给新玩家：结构化「故事回顾」
                await self.send_to(ws, {"type": "recap", "recap": self.state.recap()})
            await self.send_state()

    async def on_configure(self, name, msg, ws):
        """运行期接入模型：dm / assistant 仅房主可改；advisor 每个玩家管自己的。"""
        async with self.lock:
            changed = []
            dm_patch = clean_slot_patch(msg.get("dm"))
            if dm_patch:
                if self.owner == name:
                    merge_slot(self.dm_slot, dm_patch)
                    self._narrator = None  # 重建 Agent
                    changed.append("dm")
                else:
                    await self.send_to(
                        ws,
                        {"type": "error", "message": f"只有房主（{self.owner}）可以修改主机 DM 模型"},
                    )
            asst_patch = clean_slot_patch(msg.get("assistant"))
            if asst_patch:
                if self.owner == name:
                    merge_slot(self.assistant_slot, asst_patch)
                    self._assistant = None  # 重建 Agent（NPC 子系统下次取用时接上）
                    if self.npc is not None:
                        self.npc.set_agent(None)
                    changed.append("assistant")
                else:
                    await self.send_to(
                        ws,
                        {"type": "error", "message": f"只有房主（{self.owner}）可以修改主机小助手模型"},
                    )
            adv_patch = clean_slot_patch(msg.get("advisor"))
            if adv_patch:
                info = self.advisors.setdefault(
                    name, {"slot": dict(default_advisor_slot), "agent": None}
                )
                merge_slot(info["slot"], adv_patch)
                info["agent"] = None  # 重建
                changed.append("advisor")
            eng_patch = clean_engine_patch(msg.get("engine"))
            if eng_patch:
                if self.owner == name:
                    self.engine_slot.update(eng_patch)
                    # 引擎设置变了 → 三个 Agent 与所有玩家的小助手都得重建
                    self._narrator = None
                    self._assistant = None
                    if self.npc is not None:
                        self.npc.set_agent(None)
                    for info in self.advisors.values():
                        info["agent"] = None
                    changed.append("engine")
                else:
                    await self.send_to(
                        ws,
                        {"type": "error", "message": f"只有房主（{self.owner}）可以修改内置引擎设置"},
                    )
            await self.send_to(
                ws,
                {"type": "agent_status", "agents": self.agents_status(name), "changed": changed},
            )

    # ---- 剧情档案：只增落盘 + 滚动摘要 ----
    def _chronicle_add(self, text, *, kind, scene="", actors=None, turn=None) -> None:
        """把一条「已发生的事实」写进档案。写档案的永远是服务端，模型只读。"""
        if self.chronicle is None:
            return
        self.chronicle.add(
            text,
            turn=(self.state.turn if turn is None else turn),
            kind=kind,
            scene=scene or self.state.current_scene,
            actors=actors or [],
        )

    def _chronicle_turn(self, ctx, name: str, before_scene: str, parts: list) -> None:
        """一回行动结束后，把本轮的权威结果（场景/骰点/旁白）记进档案。"""
        if self.chronicle is None:
            return
        if self.state.current_scene != before_scene:
            cur = self.state.current()
            self._chronicle_add(
                f"队伍进入「{cur.get('title', self.state.current_scene)}」（{self.state.current_scene}）",
                kind="scene",
                scene=self.state.current_scene,
            )
        for ev in ctx.events:
            if ev.get("type") == "dice":
                dc = f"DC{ev['dc']} " if ev.get("dc") else ""
                res = "成功" if ev.get("success") else "失败"
                self._chronicle_add(
                    f"{ev.get('player', name)} 的 {ev.get('skill', '')} 检定 {dc}掷出 {ev.get('roll')}，{res}",
                    kind="dice",
                    actors=[ev.get("player", name)],
                )
        for p in parts:
            self._chronicle_add(p, kind="narration")

    async def _maybe_summarize(self) -> None:
        """每积累 SUMMARY_EVERY 条记录，就让主机小助手把新剧情并进滚动摘要。"""
        if self.chronicle is None:
            return
        total = len(self.chronicle)
        if total - self._summarized < SUMMARY_EVERY:
            return
        batch = self.chronicle.entries[self._summarized : total]
        ctx = SummaryCtx(
            previous=self.chronicle.summary,
            items=[e.get("text", "") for e in batch],
            scene=self.state.current().get("title", ""),
        )
        res = await self.assistant().run_task("summarize", ctx)
        text = str((res.data or {}).get("summary") or "").strip()
        if text:
            self.chronicle.set_summary(text, upto_turn=total)
        self._summarized = total

    async def _npc_prepare(self, name, text) -> str:
        """玩家行动前：认出被提到的 NPC，必要时采样其反应，产出给 DM 的「NPC 素材」。

        注意：给 DM 的只有「人物卡 + 当前状态快照 + 本次反应结论」，
        NPC 的历史流水账（events）留在 NPC 子系统里，绝不进 DM 上下文。
        """
        if self.npc is None:
            return ""
        self.npc.set_agent(self.assistant())
        cards = self.npc.detect(text)
        if not cards:
            return ""
        notes = []
        for card in cards:
            if self.npc.is_consequential(text):
                r = await self.npc.react(card, f"{name} 对 {card.name}：「{text}」")
                opts = "、".join(f"{o['label']}{int(round(o['weight'] * 100))}%" for o in r["options"][:5])
                notes.append(
                    f"（NPC 行为采样：{card.name} → 最可能「{r['chosen']}」；"
                    f"候选权重 {opts}；骰点 {r['roll']} / {r['total']}。请据此叙述，但不要报出数值。）"
                )
                self.npc.remember(card.id, name, text, turn=self.state.turn, polarity=_polarity(text))
                self._chronicle_add(
                    f"{card.name} 对 {name} 的举动做出反应：{r['chosen']}",
                    kind="npc",
                    actors=[card.name, name],
                )
            else:
                self.npc.remember(card.id, name, text, turn=self.state.turn, polarity=0.0)
        brief = self.npc.briefs_for(cards)
        return (brief + "\n" + "\n".join(notes)).strip()

    async def on_action(self, name, text):
        # 让界面知道「正在等模型」而不是「卡住了」。引擎在 CPU 上跑一次行动要
        # 几十秒到几分钟，期间没有任何消息往来 —— 没有这个事件，玩家只会看到
        # 一片静止，然后怀疑掉线。
        await self.broadcast(
            {"type": "thinking", "active": True, "who": "dm", "label": "DM 正在推理剧情…"}
        )
        try:
            async with self.lock:
                before_scene = self.state.current_scene
                self._chronicle_add(f"{name} 尝试：{text}", kind="action", actors=[name])
                npc_brief = await self._npc_prepare(name, text)
                ctx = NarratorContext(state=self.state, player_name=name, npc_brief=npc_brief)
                res = await self.narrator().run(ctx, text)
                for ev in ctx.events:
                    await self.broadcast(ev)
                parts = [p.strip() for p in ctx.narration_parts if p and p.strip()]
                if res.text and res.text.strip():
                    parts.append(res.text.strip())
                for p in parts:
                    await self.broadcast({"type": "narration", "author": "DM", "text": p})
                self._chronicle_turn(ctx, name, before_scene, parts)
                await self._maybe_summarize()
                await self.send_state()
                note = self._agent_note("dm", res)
                if note["source"] != "llm":  # 正常走模型不必刷屏；退化必须可见
                    await self.broadcast(note)
        finally:
            await self.broadcast({"type": "thinking", "active": False, "who": "dm"})

    async def on_suggest(self, name, ws):
        await self.send_to(
            ws, {"type": "thinking", "active": True, "who": "advisor", "label": "助手正在思考…"}
        )
        try:
            agent = self.advisor(name)
            res = await agent.suggest(AdvisorContext(state=self.state, player_name=name))
            await self.send_to(
                ws,
                {
                    "type": "suggestions",
                    "options": res.data.get("options", []),
                    "agent": {
                        "source": res.source,
                        "stopped": res.stopped,
                        "tools": res.used_tools,
                        "error": (res.error or "")[:300],
                    },
                },
            )
            note = self._agent_note("advisor", res)
            if note["source"] != "llm":
                await self.send_to(ws, note)
        finally:
            await self.send_to(ws, {"type": "thinking", "active": False, "who": "advisor"})

    @staticmethod
    def _agent_note(who: str, res) -> dict:
        """把「这一轮到底有没有真跑到模型」整理成一条可下发的事件。

        `AgentResult.source` 只有两种：llm（真的跑到了模型）/ failed（这一轮没有
        产出，原因在 `error` 里）。2026-10-13 用户拍板取消脚本化兜底之后，不再有
        "用剧本拼一段旁白冒充模型"这条路 —— 所以非 llm 时界面必须把失败明说出来。
        """
        return {
            "type": "agent_note",
            "who": who,
            "source": getattr(res, "source", "") or "",
            "stopped": getattr(res, "stopped", "") or "",
            "error": (getattr(res, "error", "") or "")[:300],
            "tools": list(getattr(res, "used_tools", []) or []),
        }

    async def on_roll(self, name):
        await self.broadcast(
            {"type": "dice", "player": name, "skill": "d20", "dc": None, "roll": roll(20), "success": None, "flag": None}
        )


rooms = {}


def gen_code():
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))


app = FastAPI(title="RPGBar", version=__version__)


@app.get("/api/version")
async def api_version():
    """版本信息，供客户端展示（版本号来自 server/__init__.py）。"""
    return {
        "name": "RPGBar",
        "version": __version__,
        "script": store.title,
        "script_path": active["rel"],
        "script_info": {
            "fmt": store.fmt,
            "structured": store.doc.structured,
            "scenes": len(store.scenes),
            "flags": len(store.flag_ids),
            "chunks": len(store.chunk_list),
            "warnings": store.warnings,
        },
        "agents": describe_agents(default_dm_slot, default_advisor_slot, default_assistant_slot),
        "memory": {
            "chronicle": CHRONICLE_ENABLED,
            "summary_every": SUMMARY_EVERY,
            "embedding": EMBEDDER.status() if EMBEDDER is not None else {"available": False, "reason": "未启用"},
        },
    }


@app.get("/api/net")
async def api_net():
    """本机可被朋友访问的地址列表（含 EasyTier / Tailscale 等虚拟网卡）。

    主机在「加入页 / 设置」里用它展示「把哪一行发给朋友」，
    省得自己去 ipconfig 里翻虚拟 IP —— 装了虚拟局域网就能直接看到对应地址。
    """
    port = _server_port()
    primary, found = _ipv4_candidates()
    return {
        "port": port,
        "primary": primary,
        "addresses": [
            {"name": name, "ip": ip, "url": f"http://{ip}:{port}", "primary": ip == primary}
            for name, ip in found
        ],
    }


@app.post("/api/models/test")
async def api_models_test(payload: dict = Body(...)):
    """探测一个模型槽位是否可用（供设置界面的「测试连接」按钮调用）。

    只做一次极短的单轮对话，确认 base_url / api_key / model 三件套能通；
    带 probe_tools=true 时再额外探一次工具调用能力（决定走工具循环还是单轮 JSON）。
    槽位字段先过 clean_slot_patch 白名单，避免脏数据。
    """
    raw = payload.get("slot") if isinstance(payload.get("slot"), dict) else payload
    slot = dict(clean_slot_patch(raw))
    slot.setdefault("timeout", 20.0)  # 测试用短超时，避免界面长时间挂着
    out = {"ok": False, "reply": "", "elapsed_ms": 0, "tool_calling": None}
    provider = build_provider(slot)
    if provider is None:
        if (slot.get("kind") or "").lower() == "engine":
            out["error"] = ("内置引擎不可用：库或权重没找到"
                            "（在服务器上跑 python scripts/engine_probe.py 自检）")
        else:
            out["error"] = "配置不完整：云 API 需要 api_key，本地模型需要 base_url 与 model"
        return out
    if slot.get("base_url"):
        provider.base_url = slot["base_url"].rstrip("/")
    t0 = time.time()
    try:
        reply = await provider.chat([{"role": "user", "content": "只回复两个字：就绪"}])
        out["reply"] = (reply or "").strip()[:80]
        out["ok"] = True
        if payload.get("probe_tools"):
            try:
                await provider.chat_with_tools(
                    [{"role": "user", "content": "请调用 ping 工具。"}],
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": "ping",
                                "description": "连通性测试",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                )
                out["tool_calling"] = True
            except ToolCallingUnsupported:
                out["tool_calling"] = False
            except Exception as e:  # noqa: BLE001
                out["tool_calling"] = None
                out["tool_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        await provider.close()
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


@app.get("/api/scripts")
async def api_scripts():
    """列出可用的剧本文件（scripts/ 目录下的 .json/.md/.txt）。"""
    return {
        "active": active["rel"],
        "active_title": store.title,
        "dirs": [str(d) for d in script_dirs()],
        "scripts": list_scripts(),
    }


def _read_payload(payload: dict):
    """把 inspect / load 的请求体变成 (ScriptStore, 显示名, 落盘路径或 None)。

    两种用法：
    - {"path": "totsk_l1.json"}          读 scripts/ 目录里的文件（仅限该目录，防任意文件读取）
    - {"content": "...", "name": "x.md"} 直接给内容（界面里粘贴/上传）
    """
    rel = payload.get("path")
    if isinstance(rel, str) and rel.strip():
        p = _safe_script_path(rel)
        if p is None:
            return None, f"找不到剧本文件「{rel}」（只允许 scripts/ 目录下的 .json/.md/.txt）", None
        try:
            return ScriptStore(p, label=_rel_of(p)), _rel_of(p), p
        except Exception as e:  # noqa: BLE001
            return None, f"读取失败：{type(e).__name__}: {e}", None

    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        name = str(payload.get("name") or "粘贴的剧本").strip()
        try:
            return ScriptStore.from_content(content, name), name, None
        except Exception as e:  # noqa: BLE001
            return None, f"解析失败：{type(e).__name__}: {e}", None
    return None, "需要提供 path（scripts/ 目录下的文件）或 content（剧本内容）", None


@app.post("/api/scripts/inspect")
async def api_scripts_inspect(payload: dict = Body(...)):
    """读取剧本 → 自动切片 → 生成次级 prompt。**不改动**当前对局。"""
    new_store, label, _ = _read_payload(payload)
    if new_store is None:
        return {"ok": False, "error": label}
    info = new_store.inspect()
    info["ok"] = True
    info["label"] = label
    info["active"] = False
    return info


@app.post("/api/scripts/load")
async def api_scripts_load(payload: dict = Body(...)):
    """读取剧本 → 切片 → 切换为「活动剧本」。

    只影响**新开的房间**：已经在玩的房间在创建时就钉住了自己的副本，不会被中途换剧本。
    """
    global store
    new_store, label, path = _read_payload(payload)
    if new_store is None:
        return {"ok": False, "error": label}
    if new_store.doc.structured and not new_store.scenes:
        return {"ok": False, "error": "剧本没有任何场景，无法开局"}
    store = new_store
    active["path"] = path or Path(label)
    active["rel"] = label
    note = "新开的房间将使用该剧本"
    if rooms:
        note += f"；当前有 {len(rooms)} 个房间仍在使用它们各自的旧剧本"
    return {
        "ok": True,
        "label": label,
        "note": note,
        **{k: v for k, v in new_store.inspect().items() if k not in ("chunks",)},
        "chunks": [c.to_dict() for c in new_store.chunk_list],
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    name = None
    room = None
    pump = None
    try:
        first = json.loads(await ws.receive_text())
        if first.get("type") != "join":
            await ws.close()
            return
        name = (first.get("name") or "冒险者").strip()[:20] or "冒险者"
        character = first.get("character") or {}
        code = (first.get("room") or "").strip().upper()
        script_rel = first.get("script")
        room = rooms.get(code) if code else None
        if room is None:
            # 新建房间：允许房主在建房时指定剧本（只认 scripts/ 目录里的文件）
            room_store = None
            if isinstance(script_rel, str) and script_rel.strip():
                p = _safe_script_path(script_rel)
                if p is not None:
                    try:
                        room_store = ScriptStore(p, label=_rel_of(p))
                    except Exception:  # noqa: BLE001
                        room_store = None
                if room_store is None:
                    print(f"[RPGBar] 忽略建房请求里无效的剧本：{script_rel!r}")
            if not code:
                code = gen_code()
            room = Room(code, room_store or store)
            rooms[code] = room
            # 本局种子进日志、不进界面：要复现某一局的剧情就从这里取数
            # （同一个种子 + 同一串输入 → 同一条随机流 → 同一个故事）。
            print(
                f"[RPGBar] 新房「{code}」本局种子 {room.seed}"
                f"（DM {derive_seed(room.seed, 'dm')}"
                f" / 小助手 {derive_seed(room.seed, 'assistant')}）"
            )
        # ---- 消息泵：把「收」与「处理」拆开 ----
        # `receive_text()` 必须一直有人在 await 着。uvicorn 的 WebSocket keepalive
        # 依赖它：一旦没人收，服务器发的 ping 就得不到 pong 处理，默认 20 秒把连接
        # 掐断（实测点「助手建议」时第 21.6 秒收到 code 1006）。而一次引擎推理要
        # 几十秒到几分钟，串在主循环里必然超时 —— 与模型快慢无关。
        #
        # 泵只负责收、主循环照旧**串行**处理消息（保持原有的顺序语义，不引入并发），
        # 所以刻意不用 create_task 去跑业务。
        inbox: asyncio.Queue = asyncio.Queue()

        async def pump():
            try:
                while True:
                    inbox.put_nowait(await ws.receive_text())
            except Exception:  # noqa: BLE001 —— 断开/异常一律当作收尾信号
                inbox.put_nowait(None)

        pump = asyncio.create_task(pump())

        # join 也可能慢（中途加入要让 DM 补剧情），放在泵之后就不怕它超时了。
        await room.on_join(name, character, ws)

        busy = False  # 本连接是否有耗时任务在跑（同一玩家开两个标签页互不影响）
        while True:
            raw = await inbox.get()
            if raw is None:
                break
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "action":
                if busy:
                    # 之前这里是静默 `continue`：玩家点了没任何反应，还以为卡死。
                    await Room.send_to(ws, {
                        "type": "busy", "who": "dm",
                        "message": "DM 正在推理这一轮，请等它结束再行动。",
                    })
                    continue
                busy = True
                try:
                    await room.on_action(name, (msg.get("text") or "").strip())
                finally:
                    busy = False
            elif t == "suggest":
                if busy:
                    await Room.send_to(ws, {
                        "type": "busy", "who": "advisor",
                        "message": "上一轮还在推理中，助手建议要排在它后面 —— 等这轮结束再点。",
                    })
                    continue
                busy = True
                try:
                    await room.on_suggest(name, ws)
                finally:
                    busy = False
            elif t == "configure":
                await room.on_configure(name, msg, ws)
            elif t == "roll":
                await room.on_roll(name)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False))
        except Exception:
            pass
    finally:
        if pump is not None:
            pump.cancel()
        if room is not None and name is not None:
            room.state.remove_player(name)
            room.clients = [(n, w) for n, w in room.clients if n != name]
            if room.clients:
                await room.broadcast({"type": "system", "text": f"{name} 离开了队伍"})
                await room.send_state()
            else:
                rooms.pop(room.code, None)


# 静态资源（app.js / style.css / index.html）改动频繁，而"改了前端、浏览器还在用
# 旧文件"这类问题排查起来极费劲（界面看着没变化，实际是缓存）。这里挂 no-cache：
# 浏览器仍走 ETag/Last-Modified 条件请求（没变就 304，很快），但一变立刻生效。
@app.middleware("http")
async def _no_cache_web(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


WEB_DIR = RESOURCE_DIR / "web"
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def _ipv4_candidates():
    """枚举本机所有可用 IPv4（含虚拟网卡），返回 (主出口 IP, [(网卡名, IP), ...])。

    三个来源取长补短：
    1. UDP 连外网探到的「主出口 IP」——最可靠地指向物理网卡；
    2. ipconfig（Windows）/ ip addr（Linux）解析——**能拿到网卡名字**，
       所以装了 EasyTier / Tailscale 之后，虚拟网卡的地址也会出现在这里；
    3. 主机名解析兜底。
    过滤掉回环与 169.254 自动配置地址。主出口 IP 排在最前。
    """
    import socket
    import subprocess

    found, seen = [], set()

    def add(name, ip):
        if ip and not ip.startswith(("127.", "169.254.")) and ip not in seen:
            seen.add(ip)
            found.append((name or "本机", ip))

    primary = None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        primary = s.getsockname()[0]
    except Exception:
        pass
    finally:
        s.close()

    def decode(raw: bytes) -> str:
        """ipconfig / ip 的输出编码随系统控制台而变，这里按 UTF-8 → GBK 依次尝试。"""
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return (raw or b"").decode("latin-1", "ignore")

    try:
        if os.name == "nt":
            raw = subprocess.run(["ipconfig"], capture_output=True, timeout=5).stdout or b""
            name = None
            for line in decode(raw).splitlines():
                if not line.strip():
                    continue
                # 网卡标题行：顶格且以冒号结尾（如「以太网适配器 EasyTier:」）
                if not line[0].isspace() and line.rstrip().endswith(":"):
                    header = line.rstrip()[:-1].strip()
                    m = re.match(r".*?(?:适配器|adapter)\s*(.*)$", header, re.I)
                    name = (m.group(1) if m else header).strip() or header
                    continue
                m = re.search(r"IPv4[^:]*:\s*(\d{1,3}(?:\.\d{1,3}){3})", line)
                if m:
                    add(name, m.group(1))
        else:
            raw = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, timeout=5).stdout or b""
            for line in decode(raw).splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[2] == "inet":
                    add(parts[1], parts[3].split("/")[0])
    except Exception:
        pass

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add("本机", ip)
    except Exception:
        pass

    if primary:
        add("主网卡", primary)
    found.sort(key=lambda kv: kv[1] != primary)  # 主出口 IP 排最前
    return primary, found


def main():
    import uvicorn

    s = cfg["server"]
    port = _server_port()
    host = "0.0.0.0" if os.environ.get("PORT") else s["host"]
    print(f"[RPGBar] v{__version__} 服务器监听 {host}:{port}")
    if host in ("0.0.0.0", "::"):
        print(f"[RPGBar] 本机访问: http://127.0.0.1:{port}")
        primary, found = _ipv4_candidates()
        if found:
            print("[RPGBar] 把下面任意一行发给朋友（局域网 / 虚拟局域网均可）：")
            for name, ip in found:
                mark = "  ← 本机主网卡" if ip == primary else ""
                print(f"[RPGBar]     http://{ip}:{port}    [{name}]{mark}")
        else:
            print("[RPGBar] 未能自动识别可用地址，请运行 ipconfig 手动查看")
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
