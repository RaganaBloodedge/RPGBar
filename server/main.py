"""FastAPI + WebSocket 多人房间服务。

房间有**两个阶段**：等待中（`waiting`）→ 房主点「开始」→ 进行中（`playing`）。
等待阶段不能行动（服务端回 `not_started`），所有人在「等待室」里等人齐；
开始之后进来的人算**中途加入**，DM 会单独补一段带入旁白。
房间「有人连线就活着」，最后一人离开即回收（`rooms.pop`）。

协议（JSON 文本帧）：
  客户端 → 服务端：
    {"type":"join","name":"...","character":{...},"room":"ABC12"(可空),"password":"...(可空)",
     "script":"...(可空，建房时选)",
     "create":{"name":"房间名(可空，缺省用剧本名)","note":"备注","max_players":4,
               "visibility":"public|private","password":"...","script":"totsk_l1.json"}}
       # room 为空 = 建房（房间名/备注/人数/可见性/密码都在 create 里）；
       # room 填了但那个房号还不存在 = 也建房（原有玩法：两人各填同一个房号就能碰头）；
       # room 填了且存在 = 加入，可能被 join_denied（密码错 / 房间满）。
    {"type":"action","text":"..."}
    {"type":"suggest"}
    {"type":"roll"}
    {"type":"configure","dm":{...}(可空),"advisor":{...}(可空),"assistant":{...}(可空)}
       # 运行期接模型；dm / assistant 仅房主可改，advisor 每人管自己的
       槽位字段：kind(off|cloud|local|engine) + base_url + model + temperature + api_key(省略=保持原值)
    {"type":"start"}                          # 仅房主：等待中 → 进行中
    {"type":"room_settings","name":"...","note":"...","max_players":4,
     "visibility":"public|private","password":"...(省略=保持原值)","clear_password":true(清空)}
       # 仅房主：只改白名单字段，没出现的字段不动
    {"type":"kick","target":"玩家名"}          # 仅房主
    {"type":"transfer","target":"玩家名"}      # 仅房主：移交房主
    {"type":"close_room"}                     # 仅房主：解散房间（所有人收到 room_closed）
    {"type":"leave"}                          # 主动离开（走正常收尾，会广播「离开了队伍」）
  服务端 → 客户端：
    {"type":"welcome","room":"...","you":"...","late":bool,"script":"剧本名","agents":{...},
      "room_info":{...},"script_info":{...},"chunks":[...],"npcs":[...],"memory":{...},
      "prompt":{"advisor":"...","dm":"...(仅房主)"}}  # 连入即同步（含本房间已有 NPC / 档案状态）
    {"type":"room","room":{...}}
      # 房间快照：房间名 / 备注 / 剧本 / 房主 / 在线人数 / 人数上限 / 阶段 / 可见性 /
      # 是否要密码 / 建房时间。人数、房主、设置、阶段任一变化都会重发一次。
      # ⚠️ 绝不含密码本身（只有 has_password 这个布尔）。
    {"type":"room_sync","agents":{...},"script_info":{...},"chunks":[...],"prompt":{...}}
      # 按人重发「剧本 + 次级 prompt + 房主是谁」。**房主换人时会发一次** ——
      # DM 的次级 prompt 只给房主，换了房主就得让新的拿到、旧的失去。
    {"type":"not_started","message":"..."}   # 等待阶段收到了 action / suggest / roll
    {"type":"join_denied","code":"full|password","message":"..."}  # 进不去，界面退回大厅
    {"type":"kicked","by":"房主名","message":"..."}                 # 被房主请出去
    {"type":"room_closed","by":"房主名","message":"..."}            # 房间被解散
    {"type":"agent_status","agents":{...},"changed":["dm","advisor","assistant","engine"]}
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
    GET  /api/rooms            大厅：列出公开房间（私密房间不上列表）+ 建房表单的缺省值
    POST /api/models/test      探测一个模型槽位（含工具调用能力）
    GET  /api/scripts          列出可用的剧本文件（含各自声明的人数）
    POST /api/scripts/inspect  读取某个剧本 → 自动切片 → 返回次级 prompt（只看不改）
    POST /api/scripts/load     读取并切换「活动剧本」（新开的房间用它）
"""
import asyncio
import atexit
import json
import os
import re
import secrets
import string
import time
from pathlib import Path

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import __version__, character_card
from .agents import (
    ActionJudgeAgent,
    AdvisorAgent,
    AdvisorContext,
    AssistantAgent,
    NarratorAgent,
    NarratorContext,
    NarrationVerifierAgent,
    SummaryCtx,
    analyze_character_card,
    describe_agents,
)
from .action_validation import candidates as action_candidates
from .action_validation import validate as validate_action
from .chronicle import Chronicle
from .config import RESOURCE_DIR, USER_DIR, load_config
from .dice import roll
from .dm import DM
from .embedding import get_embedder
from .engine import BACKENDS, derive_seed, gpu_probe, normalize_backend
from .fact_transaction import commit_world_event
from .llm import ToolCallingUnsupported, build_provider
from . import llamacpp as llamacpp_mod
from .lobby import (
    PHASE_PLAYING,
    PHASE_WAITING,
    VIS_PRIVATE,
    build_public,
    clean_create,
    clean_settings,
    password_ok,
)
from .npc import HELP_VERBS, HURT_VERBS, NpcService
from .play_definition import PlayDefinition
from .rag import ScriptStore
from .script_knowledge import ScriptKnowledgeAdapter
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
    # 客户端有的版本传 `foo.json`，有的传 `scripts/foo.json`；两者都表示
    # 资源目录下的剧本，统一归一化后再做路径安全检查。
    rel = rel.replace("\\", "/")
    if rel.lower().startswith("scripts/"):
        rel = rel[8:]
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
    play = PlayDefinition.from_store(rel, store)
    return {
        **play.to_public(),
        "file": path.name,
        "fmt": store.fmt,
        "warnings": store.warnings,
    }


def _is_internal_script(path: Path) -> bool:
    """测试夹具和开发辅助文件不进入玩家建房下拉框。

    这是文件命名约定，不是剧本内容规则；它们仍可通过精确路径被 inspect/load，
    方便开发回归测试而不污染真实玩家的剧本选择。
    """
    stem = path.stem.lower()
    return (
        stem.startswith("_")
        or stem.startswith("test_")
        or (stem.startswith("play_") and path.suffix.lower() in {".txt", ".log"})
    )


def list_scripts(include_internal: bool = False) -> list:
    seen, items = set(), []
    for d in script_dirs():
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if (p.is_file() and p.suffix.lower() in SCRIPT_SUFFIX
                    and p.name not in seen
                    and (include_internal or not _is_internal_script(p))):
                seen.add(p.name)
                items.append(_script_entry(p))
    return items


# ---- 活动剧本（room 创建时取用；切换只影响新开的房间）----
# 服务端启动时不再偷偷加载某个 demo。大厅必须让建房者选择剧本；
# active/store 仍保留一个空文档，保证版本与大厅接口可以在“尚未选本”状态下工作。
active = {"path": None, "rel": ""}
if cfg.get("script"):
    active["path"] = _resolve_script(cfg["script"])
    active["rel"] = _rel_of(active["path"])
    store = ScriptStore(active["path"], label=active["rel"])
else:
    store = ScriptStore.from_content('{"title":"未选择剧本","scenes":[]}', "")

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

if active["path"] is None:
    print("[RPGBar] 未加载默认剧本：请在大厅创建房间时选择剧本")
else:
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

# GPU 可用性：旧的 rpg_engine 走 gpu_probe；llama.cpp 以槽位 backend 为准，
# 不检查已退役的 rpg_engine_cuda.dll。
_llama_gpu = any(
    a.get("kind") in ("llamacpp", "llama", "llama_cpp")
    and (a.get("llamacpp") or {}).get("backend") == "gpu"
    and a.get("ready")
    for a in _agents.values()
)
_gpu = {"available": True, "devices": 0, "compute_cap": "", "lib": ""} if _llama_gpu else gpu_probe()
if _llama_gpu:
    print("[RPGBar] llama.cpp GPU 后端已配置：首次生成时由 llama-server 验证 CUDA")
elif _gpu["available"]:
    # 算力可能查不到（老驱动没有 cuDeviceComputeCapability）—— 那就只说设备数，
    # 不编一个"未知"出来（查不到 ≠ 有问题）。
    _cap = f" · 算力 {_gpu['compute_cap']}" if _gpu.get("compute_cap") else ""
    print(f"[RPGBar] GPU 后端可用：{_gpu['devices']} 个 CUDA 设备{_cap} · {_gpu['lib']}")
else:
    print(f"[RPGBar] GPU 后端不可用（{_gpu['reason']}）"
          "—— 界面上的「GPU」选项会置灰，只能选 CPU。")


# ---- 模型槽位的清洗与合并 ----
ALLOWED_KINDS = ("off", "cloud", "local", "engine", "llamacpp", "llama", "llama_cpp")


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
    def __init__(self, code, script_store=None, create=None, script_rel=""):
        self.code = code
        # 这个房间钉住的剧本（相对 scripts/ 的路径）—— 知识库按**剧本**取，不全局共用
        self.script_rel = script_rel or ""
        # 房间在创建时「钉住」当时的活动剧本：之后服务端换剧本，进行中的对局不受影响。
        self.store = script_store or store
        if not self.script_rel:
            source = str(getattr(self.store, "source", "") or "")
            self.script_rel = Path(source).name if source else ""
        self.play = PlayDefinition.from_store(self.script_rel, self.store)
        # Agent 只读这份玩法定义，不再各自从 data 推导 mode/rules。
        self.store.play_definition = self.play
        # 编译器产出的知识库（五类容器）。**整局共用**，DM 只经 kb_lookup/kb_lore 读它；
        # 没编译过库的机器上这里是 None，DM 少两个工具，游戏照常跑。
        try:
            from .kb_tools import kb_store as _kb_store

            self._kb = _kb_store(self.script_rel)
        except Exception:  # noqa: BLE001
            self._kb = None
        self.knowledge = ScriptKnowledgeAdapter(self.store, self._kb)
        self.store.script_knowledge = self.knowledge
        # 判定引擎：读知识库目录旁的 rule_profile.json（COC → d100 掷低）。
        # 没有档案就是 None，DM 退回 d20 —— 老剧本不受影响。
        self.rules = None
        try:
            from .rules import Rules

            self.rules = Rules.load(getattr(self._kb, "path", None))
            if getattr(self, "rules", None) is not None:
                print(f"[RPGBar] 判定档案：{self.rules.describe()}")
        except Exception as exc:  # noqa: BLE001
            print(f"[RPGBar] 判定档案加载失败（退回 d20）：{exc}")
        # 这局的世界层：独立库（data/worlds/<剧本>/<房间>.sqlite）。
        # 剧本只提供**种子场景**，之后世界怎么长由这一局自己记。
        self.world = None
        try:
            from .world import World

            self.world = World.for_room(self.script_rel or "", code,
                                        kb=(str(self._kb.path) if self._kb else ""))
            self._seed_world()
        except Exception as exc:  # noqa: BLE001
            print(f"[RPGBar] 世界层初始化失败（房间照常，DM 少了现场记忆）：{exc}")
        # ---- 大厅元数据（建房时由房主填，之后房主可以改）----
        # 建房参数先过 lobby.clean_create：它保证这里拿到的**一定是一套完整值**，
        # 而且人数上下限是按**本房间钉住的那份剧本**算的（剧本可选的 players 段，
        # 没写就是 1–4 人）。
        params, _ = clean_create(create or {}, self.store.players)
        # 房间名缺省 = 剧本名：陌生人扫列表时看到的是剧本，不是一个空字符串。
        self.name = params["name"] or self.store.title
        self.note = params["note"]
        self.min_players = params["min_players"]
        self.max_players = params["max_players"]
        self.visibility = params["visibility"]
        # 密码只存在服务端、只用于校验；下发的一律只有 has_password 这个布尔。
        self.password = params["password"]
        # 等待中 → 房主点「开始」→ 进行中。开局前不能行动（见 require_playing）。
        self.phase = PHASE_WAITING
        self.created_at = time.time()
        # 被房主踢掉 / 房间被解散的名字：这些人的断开**不该**再广播一次「离开队伍」
        # （服务端/房主已经宣布过了），也不该触发「最后一人走了就销毁房间」。
        self.ejected = set()
        self.closed = False
        # 本局随机种子：建房时掷一次、整局固定（引擎是纯函数，不给种子则每个房间
        # 每一局的随机流都一模一样 —— "换个房间重开"其实拿到同一串随机数）。
        # 只进服务端日志、不下发：想复现某一局的剧情，从日志里取这个数。
        self.seed = secrets.randbits(63)
        self.state = GameState(self.store, self.store.start_scene)
        self.clients = []  # list[(name, websocket)]
        self.lock = asyncio.Lock()
        self.owner = None  # 房主（第一个加入的人；建房者）才能改 DM / 房间设置
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
        # 角色卡：这个本要不要玩家填卡，由模型读剧本后给出（剧本级结论，按内容缓存）。
        # status 三态：pending（还没分析）/ ready（有结论）/ failed（这一轮没产出）。
        # card 为 None 不代表「不需要」——「不需要」是一张 required=false 的卡。
        self.card_state = {"status": "pending", "card": None, "error": ""}
        self._card_lock = asyncio.Lock()
        self._card_task = None
        # 预热：缓存命中时 GPU 是闲的，顺手把「首次前向」的成本付掉（见 _warm）。
        self._warm_task = None

    # ---- 大厅：房间的公开信息 / 容量 / 阶段 ----
    def lobby_info(self) -> dict:
        """本房间对外的公开信息（大厅列表项与 `room` 事件共用同一份形状）。

        ⚠️ 密码本身**不出现在这里**（只有 `has_password` 布尔）；剧本内幕、
        模型槽位、api_key 同理，一律不下发。
        """
        return build_public(
            code=self.code,
            name=self.name,
            note=self.note,
            script_title=self.store.title,
            owner=self.owner,
            online=len(self.clients),
            max_players=self.max_players,
            phase=self.phase,
            visibility=self.visibility,
            has_password=bool(self.password),
            created_at=self.created_at,
        )

    async def broadcast_room(self) -> None:
        """把房间快照广播出去 —— 人数 / 房主 / 设置 / 阶段任何一项变了都该发一次。"""
        await self.broadcast({"type": "room", "room": self.lobby_info()})

    # ---- 角色卡：剧本分析 ----

    def card_public(self) -> dict:
        """角色卡对外的状态（下发给等待室）。

        ⚠️ `skill_map` 刻意**不下发**：它写着「哪个检定吃哪个属性」，等于把
        「这个本有哪些检定」摊开给玩家，与 `dm_notes` 同一档内幕。
        """
        card = self.card_state.get("card")
        return {
            "status": self.card_state.get("status", "pending"),
            "error": self.card_state.get("error", ""),
            "needs_card": bool(card and card.get("required")),
            "card": character_card.to_client(card) if card else None,
        }

    def _kick_card(self, force: bool = False) -> None:
        """后台起一次角色卡分析（不阻塞这一轮的 join / 配置）。

        分析是一次完整的模型调用（本地引擎上几十秒），等它会把「进房」卡住；
        结果到了 `ensure_card` 自己广播。已经有一次在跑就不重复起。
        """
        if self._card_task is not None and not self._card_task.done():
            return
        self._card_task = asyncio.create_task(self.ensure_card(force=force))

    async def ensure_card(self, force: bool = False) -> None:
        """保证这张角色卡已经分析过（缓存优先），并把结果广播给等待室。

        为什么用 **DM 槽位**的模型来做：要开局就必须有一个 DM 模型，所以
        「没有模型就没有角色卡」不会造出「能玩但没卡」的怪状态；换成助手槽位就会有这个缺口。

        `force=True` 允许从失败里重来 —— 房主常常是**先进房再配模型**的，
        第一次分析多半就是撞在「还没配模型」上，不重试的话这张卡永远出不来。
        """
        async with self._card_lock:
            if self.card_state["status"] == "ready" and not force:
                return
            key = character_card.card_key(self.store)
            cached = character_card.load_cached(key)
            if cached is not None:
                self._apply_card(cached)
                await self.broadcast({"type": "character_schema", **self.card_public()})
                # 缓存命中 = 这张卡不花模型调用，GPU 是闲的。而 DM 的第一次生成要付
                # 一笔「首次前向」的固定成本（实测 2000 token 的 prompt 要 26 秒，
                # 之后同样长度只要 7–10 秒）。玩家此刻正在等待室里没事干 —— 挪到这里。
                self._kick_warm()
                return
            # 先告诉等待室「在分析」，免得大家盯着一片空白以为坏了
            self.card_state.update(status="pending", error="")
            await self.broadcast({"type": "character_schema", **self.card_public()})
            res = await analyze_character_card(self.store, self.narrator().provider)
            if res.source == "llm" and res.data:
                character_card.save_cached(key, res.data, title=self.store.title)
                self._apply_card(res.data)
            else:
                self.card_state.update(
                    status="failed", card=None,
                    error=res.error or "模型没有给出可用的角色卡",
                )
            await self.broadcast({"type": "character_schema", **self.card_public()})

    def _apply_card(self, card) -> None:
        self.card_state.update(status="ready", card=card, error="")
        # 卡片挂在**剧本**上：旁白 Agent 投检定时从 `store.card_schema` 取属性加值。
        # 同一份剧本内容算出来的卡必然相同，所以多个房间共用一个 store 也不会串味。
        self.store.card_schema = card

    # ---- 预热 ----
    def _kick_warm(self) -> None:
        """踢一次后台预热（已经有一个在跑就跳过）。"""
        if self._warm_task is not None and not self._warm_task.done():
            return
        self._warm_task = asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        """在等待室里空转一次 DM 的前向，把「首次 prefill」的成本提前付掉。

        只对**内置引擎**有意义（云 API 的首字延迟不归我们管）：`NarratorAgent.warmup()`
        会自己判断 `provider.needs_warmup`，不是引擎就直接返回。

        为什么允许它和开局抢 GPU：最坏情况是「预热先拿到锁 → 第一轮等它」，总耗时与
        「没预热、让第一轮自己付这笔钱」**完全等价**；最好情况是省掉 20 多秒。只赢不输。
        """
        try:
            await self.narrator().warmup(
                NarratorContext(state=self.state, player_name="（预热）")
            )
        except Exception:  # noqa: BLE001  预热失败不影响任何东西
            pass

    def join_denied(self, name, password):
        """能不能进这间房 —— 返回拒绝原因（None = 放行）。

        顺序有讲究：**先密码、后容量**。房间满了但密码也不对的时候，不该先告诉对方
        "满了" —— 那等于确认了"密码对了就有位置"，把容量当成了密码的旁路。

        容量这条只拦**新面孔**：已经在 `state.players` 里的人（断线重连 / 换标签页）
        照旧放行，否则满员的房间会把掉线的人永久关在外面。
        """
        if not password_ok(self.password, password):
            return {"code": "password", "message": "这间房设了密码，请填对密码再进。"}
        if len(self.clients) >= self.max_players and name not in self.state.players:
            return {
                "code": "full",
                "message": f"房间已满（{len(self.clients)}/{self.max_players}），等有人退出再来。",
            }
        return None

    async def require_playing(self, ws) -> bool:
        """玩法动作的阶段门控：等待中不放行任何行动 / 建议 / 掷骰。

        等待阶段既没开局、也没有剧情可推进，放行只会让 DM 去猜"现在该讲什么"。
        这里直接回一条 `not_started`，界面据此提示"等房主开始"。
        """
        if self.phase == PHASE_PLAYING:
            return True
        await self.send_to(
            ws,
            {
                "type": "not_started",
                "message": "房主还没开始这一局 —— 先在等待室等人齐，房主点「开始」后再行动。",
            },
        )
        return False

    async def resync_context(self) -> None:
        """按人重发「剧本 + Agent 次级 prompt + 房主是谁」。

        房主换人时必须发一次：DM 的次级 prompt 只发给房主，换了房主就得让新的拿到、
        旧的失去（客户端按 `agents.owner` 决定设置卡片能不能改）。
        """
        for n, w in list(self.clients):
            try:
                await self.send_to(w, {
                    "type": "room_sync",
                    "agents": self.agents_status(n),
                    **self.room_context(n),
                })
            except Exception:  # noqa: BLE001
                pass
        await self.send_state()

    @staticmethod
    async def _reject(ws, message) -> None:
        """房主权限不够 / 操作不合法时的统一回话。"""
        await Room.send_to(ws, {"type": "error", "message": message})

    # ---- 大厅：房主操作 ----
    async def on_start(self, name, ws=None) -> None:
        """房主点「开始」：等待中 → 进行中，DM 出场念开场。

        门槛只做**软提示**：人数低于剧本声明的 min 时照样开始（房主说了算），
        但会广播一句说明 —— 剧本的 min 是"建议"，不是"许可证"。
        """
        async with self.lock:
            if self.owner != name:
                await self._reject(ws, f"只有房主（{self.owner}）可以开始游戏")
                return
            if self.phase == PHASE_PLAYING:
                await self._reject(ws, "这一局已经开始了")
                return
            n = len(self.clients)
            self.phase = PHASE_PLAYING
            await self.broadcast({"type": "system", "text": f"房主 {name} 开始了这一局"})
            if n < self.min_players:
                await self.broadcast({
                    "type": "system",
                    "text": f"（剧本《{self.store.title}》建议至少 {self.min_players} 人，现在只有 {n} 人 —— 房主决定照常开始。）",
                })
            await self.broadcast_room()
            # ---- session zero：沙盒剧本开局前先读库、定调、铺场景 ----
            try:
                sandbox = self.play.generation_policy == "session_zero"
                if sandbox and self.world is not None and not self.world.get("bible"):
                    from .session_zero import prepare, summary_text

                    await self.broadcast({"type": "thinking", "active": True, "who": "dm",
                                          "label": "DM 正在读规则书、准备开局…"})
                    try:
                        from types import SimpleNamespace
                        session_events = SimpleNamespace(state=self.state, fact_events=[])
                        bible = await prepare(self.store, self._kb, self.world,
                                              self.narrator().provider, play=self.play,
                                              event_ctx=session_events)
                        for fact in session_events.fact_events:
                            await self.broadcast({"type": "fact_event", "event": fact})
                        await self.broadcast({"type": "system",
                                              "text": summary_text(bible)})
                        if bible.get("hooks"):
                            await self.broadcast({"type": "system", "text":
                                                  "可动的方向：" + "；".join(bible["hooks"])})
                    finally:
                        await self.broadcast({"type": "thinking", "active": False, "who": "dm"})
            except Exception as exc:  # noqa: BLE001  开局准备失败不该挡住开玩
                print(f"[RPGBar] session zero 失败（照常开局）：{exc}")
            opening = self.state.current().get("public_text", "")
            if opening:
                await self.broadcast({"type": "narration", "author": "DM", "text": opening})
                self._chronicle_add(opening, kind="narration")
            await self.send_state()

    async def on_room_settings(self, name, msg, ws) -> None:
        """房主改房间设置（房间名 / 备注 / 人数上限 / 可见性 / 密码）。"""
        async with self.lock:
            if self.owner != name:
                await self._reject(ws, f"只有房主（{self.owner}）可以修改房间设置")
                return
            patch = clean_settings(msg, {"min_players": self.min_players}, self.store.players)
            changed, labels = [], {
                "name": "房间名", "note": "备注", "max_players": "人数上限",
                "visibility": "可见性", "password": "密码",
            }
            for key, value in patch.items():
                if getattr(self, key, None) != value:
                    setattr(self, key, value)
                    changed.append(labels.get(key, key))
            if not changed:
                await self.send_to(ws, {"type": "error", "message": "房间设置没有变化"})
                return
            await self.broadcast({"type": "system", "text": "房主更新了房间设置：" + "、".join(changed)})
            await self.broadcast_room()
            if len(self.clients) > self.max_players:
                await self.broadcast({
                    "type": "system",
                    "text": f"（人数上限现在是 {self.max_players}，当前 {len(self.clients)} 人 —— 不会赶人，但新玩家进不来。）",
                })

    async def on_kick(self, name, target, ws) -> None:
        async with self.lock:
            if self.owner != name:
                await self._reject(ws, f"只有房主（{self.owner}）可以踢人")
                return
            target = (target or "").strip()
            if not target:
                await self._reject(ws, "没指定要请出谁")
                return
            if target == self.owner:
                await self._reject(ws, "房主不能把自己请出去 —— 想结束就点「解散房间」")
                return
            hits = [w for n, w in self.clients if n == target]
            if not hits:
                await self._reject(ws, f"「{target}」不在这个房间里")
                return
            # 先登记再移人：否则这些人断开时会被当成"普通离开"，多广播一条「离开了队伍」，
            # 而且最后一人走掉时还会把房间一起销毁。
            self.ejected.add(target)
            self.state.remove_player(target)
            self.clients = [(n, w) for n, w in self.clients if n != target]
            self.advisors.pop(target, None)
            for w in hits:
                await self.send_to(w, {
                    "type": "kicked", "by": name,
                    "message": f"你被房主 {name} 请出了房间。",
                })
                # 服务端主动断开：只靠前端"收到 kicked 自己关"是有洞的 ——
                # 一个不听话的客户端（或旧版前端）会永远停在游戏屏上，而且它已经
                # 不在 clients 里了，再也收不到任何 broadcast。断在这里最省事。
                try:
                    await w.close()
                except Exception:  # noqa: BLE001
                    pass
            await self.broadcast({"type": "system", "text": f"{target} 被房主请出了房间"})
            await self.broadcast_room()
            await self.send_state()

    async def on_transfer(self, name, target, ws) -> None:
        """把房主转给别人。"""
        async with self.lock:
            if self.owner != name:
                await self._reject(ws, f"只有房主（{self.owner}）可以移交房主")
                return
            target = (target or "").strip()
            if target not in [n for n, _ in self.clients]:
                await self._reject(ws, f"「{target}」不在这个房间里")
                return
            if target == self.owner:
                await self._reject(ws, f"{target} 已经是房主了")
                return
            self.owner = target
            await self.broadcast({"type": "system", "text": f"房主已移交给 {target}"})
            # DM 的次级 prompt 只发给房主 ⇒ 换了房主必须按人重发一次（新的拿到、旧的失去）
            await self.resync_context()
            await self.broadcast_room()

    async def on_close_room(self, name, ws) -> None:
        """房主解散房间：所有人收到通知，房间立刻从大厅消失。"""
        async with self.lock:
            if self.owner != name:
                await self._reject(ws, f"只有房主（{self.owner}）可以解散房间")
                return
            self.closed = True
            await self.broadcast({
                "type": "room_closed", "by": name,
                "message": f"房主 {name} 解散了房间。",
            })
            rooms.pop(self.code, None)
            # 主动清空，免得每个客户端断开时再来一轮「离开了队伍」的广播
            # （最后一人走掉还会顺手把已经销毁的房间再销毁一次）。
            for n, w in list(self.clients):
                self.ejected.add(n)
                try:
                    await w.close()
                except Exception:  # noqa: BLE001
                    pass
            self.clients = []
            self.state.players = {}
            print(f"[RPGBar] 房间「{self.code}」已由房主 {name} 解散")

    # ---- Agent 生命周期 ----
    def _effective_slot(self, slot):
        """把「内置引擎」小节的设置并入 engine 槽位 —— **引擎段只做缺省**。

        2026-10-22 改的合并方向（原来反着：引擎段无条件覆盖槽位）。原因：
        8 GiB 卡上要「4B DM 走 GPU + 1.7B 助手走 CPU」，而后端 / 上下文本可以
        按槽位分工 —— 被覆盖一次，槽位写的 backend 就永远无效，只能全 GPU 或全 CPU。
        新语义三条：

        - **槽位显式写了的键，槽位说了算**（backend / context / max_tokens / think / threads）；
        - 槽位没写的键，用引擎段的缺省值（界面「内置引擎」改的缺省仍然生效）；
        - `enabled` 是**总开关**，只认引擎段：它关掉 = 所有 engine 槽位都没有模型
          （槽位不能反着开 —— 那会让「关掉内置引擎」变成可按住的开关）。

        并入只发生在内存里的一次拷贝上：槽位本身不动（它还要原样描述给前端）。
        """
        if (slot.get("kind") or "off").lower() != "engine":
            return slot
        if not self.engine_slot.get("enabled", True):
            return None
        merged = dict(slot)
        merged.setdefault("enabled", True)
        for key in ("think", "context", "max_tokens", "backend", "threads"):
            if key not in merged and key in self.engine_slot:
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

    def _seed_world(self) -> None:
        """把剧本的起始场景写进世界层（只写一次；已有世界则原样恢复）。"""
        w = self.world
        if w is None or w.scene() is not None:
            return
        try:
            sc = self.store.get_scene(self.store.start_scene)
        except Exception:  # noqa: BLE001
            sc = None
        if not sc:
            return
        w.add_scene(sc["id"], sc.get("title") or sc["id"], sc.get("location") or "",
                    sc.get("public_text") or "", source="script")
        w.enter(sc["id"])
        # 开局定在第一天 08:00（白天）。之后由 advance_time 推进昼夜与天数。
        if not w.get("day"):
            w.set("day", 1)
            w.set("minutes", 8 * 60)
            w.set("phase", "day")
        print(f"[RPGBar] 世界层就绪：{w.path}（起始场景 {sc['id']}）")

    def narrator(self) -> NarratorAgent:
        if self._narrator is None:
            self._narrator = NarratorAgent(
                self.store, self._provider(self.dm_slot, "dm"),
                npc=self.npc, chronicle=self.chronicle,
                kb=self._kb, world=self.world, rules=self.rules,
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
        dm_status = status.get("dm") or {}
        # 运行时默认通道是 llama.cpp；GPU 能力应以该槽位请求的后端为准。
        # gpu_probe() 只检查已退役的 rpg_engine_cuda.dll，不能拿来判断
        # llama-server 的 -ngl 99 是否可用，否则会把可用 GPU 误报成不可用。
        detail = dm_status.get("engine") or dm_status.get("llamacpp") or {}
        eng = self.engine_slot
        req_backend = normalize_backend(
            detail.get("backend") or eng.get("backend")
        )
        gpu = gpu_probe()
        if dm_status.get("kind") in ("llamacpp", "llama", "llama_cpp"):
            gpu = {
                "available": bool((dm_status.get("llamacpp") or {}).get("backend") == "gpu"
                                   and (dm_status.get("llamacpp") or {}).get("ready")),
                "reason": "llama.cpp GPU 后端将在首次生成时由 llama-server 验证"
                          if not ((dm_status.get("llamacpp") or {}).get("ready")) else "",
                "devices": 0,
                "compute_cap": "",
            }
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
            **self.play.to_public(),
            "fmt": self.store.fmt,
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
        if self.world is not None:
            # 世界层的现场实体和动态关系是当前局事实；放进公开快照，
            # 让验收器与 ActionJudge 使用同一份状态证据。
            try:
                evidence = snap.setdefault("scene_evidence", {})
                evidence["present_entities"] = [
                    {"name": e.get("name", ""), "kind": e.get("kind", ""),
                     "detail": e.get("detail", "")}
                    for e in self.world.present()[:24]
                ]
                evidence["dynamic_relations"] = self.world.relations(limit=24)
            except Exception:  # noqa: BLE001
                pass
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
            # 阶段决定这次是「一起等开局」还是「中途加入」：
            # 等待中进来 = 一起在等待室等人齐；进行中进来 = 中途加入，要让 DM
            # 单独补一段带入旁白 + 一份私有回顾（老玩家也能看到那段旁白）。
            late = self.phase == PHASE_PLAYING
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
                    # 大厅元数据：房间名 / 备注 / 人数 x/y / 阶段 / 房主 / 是否要密码。
                    # 连入即拿，界面不用再单独问一次。
                    "room_info": self.lobby_info(),
                    # 连入即同步：本房间的剧本 + 两份 Agent 次级 prompt（DM 那份仅房主可见）
                    **self.room_context(name),
                    # 本房间已有的 NPC（人物卡）——晚加入也能认识在场的人
                    "npcs": self.npc.snapshot() if self.npc is not None else [],
                    # 本房间的剧情档案状态（已记录多少条 / 是否已有滚动摘要）
                    "memory": self.memory_status(),
                    # 角色卡：这个本要不要填卡、属性有哪些（还没分析完就带 status=pending）
                    "card_state": self.card_public(),
                },
            )
            if not late:
                await self.broadcast({"type": "system", "text": f"{name} 加入了队伍"})
                if self.phase == PHASE_WAITING:
                    # 等待室不是"卡住了"：明说现在在等人齐、且开局前不能行动。
                    await self.broadcast({
                        "type": "system",
                        "text": f"等待房主开始（{len(self.clients)}/{self.max_players} 人）"
                                "—— 开局前还不能行动。",
                    })
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
            # 人数变了就得重播一次房间快照 —— 等待室里「3/4 人」那个数字、以及列表项
            # 的 online 字段都只从快照来（`state.players` 里给不出 max_players）。
            # 新人自己那份走的是 welcome 里的 room_info，这里主要是给**房里已有的人**更新。
            await self.broadcast_room()
            await self.send_state()
            # 角色卡分析只做一次（结果按剧本内容缓存）。第一次有人进房就顺手起一次，
            # 之后除非上一次失败了，否则不再调模型。
            if self.card_state["status"] in ("pending", "failed"):
                self._kick_card(force=self.card_state["status"] == "failed")

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
                    # 房主多半是**先进房再配模型**：之前那次分析很可能就是撞在
                    # 「还没配模型」上，换了 DM 模型就再试一次（已有卡则不动）。
                    if self.card_state["status"] in ("pending", "failed"):
                        self._kick_card(force=True)
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
        for fact in ctx.fact_events:
            label = fact.get("text") or (
                f"{fact['actor']} {fact['type']} {fact['target']}"
                + (f"：{'成功' if fact['success'] else '失败'}" if "success" in fact else "")
            )
            self.chronicle.add(
                label, entry=fact["id"] + "-" + self.code,
                turn=fact["turn"], kind="event", scene=fact["scene_after"],
                actors=[fact["actor"]] if fact.get("actor") else [],
                event_type=fact["type"], facts_added=fact["facts_added"],
                facts_removed=fact["facts_removed"], source="server",
                scene_before=fact["scene_before"], scene_after=fact["scene_after"],
                target=fact["target"], success=fact.get("success"),
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
        t_round = time.time()
        try:
            async with self.lock:
                before_scene = self.state.current_scene
                self._chronicle_add(f"{name} 尝试：{text}", kind="action", actors=[name])
                t_npc = time.time()
                npc_brief = await self._npc_prepare(name, text)
                # 知识库命中：玩家话里出现库里名字就确定性注入（不等模型自己想起调工具）
                kb_brief = ""
                if getattr(self, "_kb", None) is not None:
                    try:
                        kb_brief = self._kb.related(text)
                    except Exception:  # noqa: BLE001
                        kb_brief = ""
                ctx = NarratorContext(state=self.state, player_name=name,
                                      npc_brief=npc_brief, kb_brief=kb_brief)
                # 行动裁定器接收切分器和世界层的结构化接口：场景 NPC、现场实体、
                # 静态关系边、以及本局已经提交的动态关系。它不再只看玩家原话。
                current_scene = self.state.current() or {}
                # 所有静态行动证据统一由 ScriptKnowledgeAdapter 组装；World 只补
                # 当前局的动态实体/关系，不把编译器状态表误当作本局事实。
                evidence = self.knowledge.action_evidence(
                    self.state.current_scene, flags=self.state.flags, world=self.world)
                judge_context = {
                    "present_entities": evidence.get("present_entities", []),
                    "static_relations": self.knowledge.static_relations(
                        [n.get("name") for n in evidence.get("npcs", [])]
                        + [e.get("name") for e in evidence.get("present_entities", [])]
                    ),
                    "dynamic_relations": evidence.get("dynamic_relations", []),
                    "candidate_checks": [], "candidate_exits": [],
                }
                current_scene = dict(current_scene)
                current_scene["exits"] = evidence.get("exits", [])
                candidate_checks, candidate_exits = action_candidates(current_scene, text)
                judge_context["candidate_checks"] = candidate_checks
                judge_context["candidate_exits"] = candidate_exits
                # 独立行动裁定 Agent：只做语义归类，不持有任何写工具。
                # 模型失败时继续使用现有确定性门，保证本地小模型不可用也能玩。
                judge_provider = self.narrator().provider
                judgement = await ActionJudgeAgent(
                    judge_provider if judge_provider is not None
                    and judge_provider.__class__.__module__.startswith("server.llm")
                    else None
                ).judge(self.state.current() or {}, text, context=judge_context)
                # 回顾/事实问句优先走状态与记忆回答，不能被模型误判成当前场景检定。
                # 这是语言意图边界，不依赖任何剧本关键词。
                fact_question = bool(re.search(
                    r"(?:刚才|之前|此前).{0,8}(?:发生了什么|做了什么|发生过什么)|"
                    r"记得吗|回忆一下", text or ""
                ))
                plan = validate_action(current_scene, text, judgement, fact_question)
                if plan.intent == "check":
                    ctx.forced_check_id = plan.check_id
                elif plan.intent == "move":
                    # 记录给模型，真正移动仍只能由 move_to 工具执行。
                    ctx.world_digest = (ctx.world_digest + "\n行动裁定：玩家意图是移动到 "
                                        + plan.exit_scene).strip()
                elif plan.intent == "blocked":
                    ctx.oob_verb = text[:24]
                # 候选、目标合法性和事实问句优先级已经由 action_validation 统一处理。
                # **状态问句直接由世界层回答**：问方位/进度不需要模型（也不会复读）
                if self.world is not None:
                    try:
                        ans = self.world.state_answer(text)
                    except Exception:  # noqa: BLE001
                        ans = ""
                    if ans:
                        await self.broadcast({"type": "narration", "author": "DM", "text": ans})
                        self._chronicle_add(ans, kind="narration")
                        await self.send_state()
                        return
                moved = ""
                if self.world is not None and self.play.is_sandbox:
                    # **移动由服务端执行**：玩家说"去码头"，4B 会把它当断言然后否认
                    # （实测回「这里没有码头」）。地名提取与建场景都是几何，不该赌模型。
                    try:
                        place = self.world.move_intent(text)
                    except Exception:  # noqa: BLE001
                        place = ""
                    if place:
                        n = int(self.world.get("gen_count", "0") or 0) + 1
                        sid = f"gen{n}"
                        self.store.scenes[sid] = {
                            "id": sid, "chapter": 1, "title": place, "location": place,
                            "public_text": "", "dm_notes": "", "npcs": [],
                            "keywords": [place], "flags_required": [], "flags_set": [],
                            "checks": [], "exits": [],
                        }
                        fact, result, error = commit_world_event(
                            ctx, "scene_entered", actor=name, target=sid,
                            mutation=lambda: self.world.grow_place(
                                place, parent=self.state.current_scene),
                            world=self.world, allow_generated=True,
                            decorate=lambda f, r: f.update(target=str(r)),
                        )
                        if error:
                            self.store.scenes.pop(sid, None)
                            print(f"[RPGBar] 沙盒移动事务失败：{error}", flush=True)
                        else:
                            sid = str(result)
                            moved = place
                            ctx.move_used = True
                            await self.broadcast({"type": "system", "text": f"进入：{place}"})
                if self.world is not None:
                    _fact, _result, error = commit_world_event(
                        ctx, "time_advanced", actor=name, target="turn",
                        facts_added=[f"world_turn:{int(self.world.get('turn', '0') or 0) + 1}"],
                        mutation=lambda: (self.world.bump_turn(), True)[1],
                        world=self.world,
                        decorate=lambda f, _r: f.update(facts_added=[
                            f"world_turn:{self.world.get('turn', '0')}"
                        ]),
                    )
                    if error:
                        print(f"[RPGBar] 世界回合事务失败：{error}", flush=True)
                    ctx.world_present = self.world.present_text()
                    ctx.world_digest = self.world.digest_text()
                    ctx.world_unresolved = self.world.unresolved(
                        text, kb_names=(self._kb.name_index() if self._kb else None))
                if moved:
                    ctx.world_move = moved
                res = await self.narrator().run(ctx, text)
                verifier = NarrationVerifierAgent(
                    self.narrator().provider
                    if self.narrator().provider is not None
                    and self.narrator().provider.__class__.__module__.startswith("server.llm")
                    else None
                )
                verification = await verifier.verify(ctx, text, res.text or "")
                if not verification.accepted:
                    # 权威事件仍然保留；只丢弃未经确认的模型补充。
                    res.text = ""
                    res.drop_reason = "旁白审查：" + "；".join(verification.issues)
                print(f"[RPGBar] 一轮 dm {time.time()-t_round:5.1f}s"
                      f"（NPC 预备 {time.time()-t_npc:4.1f}s · 生成 {len(res.steps)} 次"
                      f" · 工具 {'/'.join(res.used_tools) or '无'}）", flush=True)
                if getattr(res, "drop_reason", ""):
                    print(f"[RPGBar] 本轮旁白被丢弃：{res.drop_reason}", flush=True)
                for fact in ctx.fact_events:
                    await self.broadcast({"type": "fact_event", "event": fact})
                for ev in ctx.events:
                    await self.broadcast(ev)
                # 系统已经广播的检定/场景权威文案不应被模型整段改写再播一次。
                # 同一轮保留第一份事实，模型只补充不重复的衔接句。
                parts = []
                for raw in ctx.narration_parts:
                    p = (raw or "").strip()
                    if p and p not in parts:
                        parts.append(p)
                if res.text and res.text.strip():
                    extra = res.text.strip()
                    # 失败检定的结果已经是完整事实边界；模型追加的“也许成功了/
                    # NPC 其实透露了线索”会把失败改写成成功，整段补写必须丢弃。
                    failure_fact = any(
                        marker in fact for fact in parts
                        for marker in ("**失败**", "一无所获", "什么也没发现", "没有发现")
                    ) or any(
                        ev.get("type") == "dice" and ev.get("success") is False
                        for ev in ctx.events if isinstance(ev, dict)
                    )
                    if failure_fact:
                        extra = ""
                    import difflib
                    # 4B 常会把 roll_check/move_to 的权威文案再完整改写一遍，
                    # 仅比较整段文本会因后面多了氛围句而漏掉。按句比较，丢掉
                    # 与服务端事实高度相似的句子，只保留真正新增的感官描写。
                    authoritative_sentences = []
                    for fact in parts:
                        authoritative_sentences.extend(
                            x.strip() for x in re.split(r"(?<=[。！？!?；;])", fact) if x.strip()
                        )
                    kept = []
                    for sentence in re.split(r"(?<=[。！？!?；;])", extra):
                        sentence = sentence.strip()
                        if not sentence:
                            continue
                        if any(
                            sentence == fact or sentence in fact or fact in sentence
                            or difflib.SequenceMatcher(None, sentence[:180], fact[:180]).ratio() >= 0.62
                            for fact in authoritative_sentences
                        ):
                            continue
                        kept.append(sentence)
                    extra = "".join(kept).strip()
                    if not any(
                        extra == p or extra in p or p in extra
                        or difflib.SequenceMatcher(None, extra[:500], p[:500]).ratio() >= 0.72
                        for p in parts
                    ):
                        parts.append(extra)
                for p in parts:
                    await self.broadcast({"type": "narration", "author": "DM", "text": p})
                self._chronicle_turn(ctx, name, before_scene, parts)
                await self._maybe_summarize()
                await self.send_state()
                note = self._agent_note("dm", res)
                if note["source"] not in ("llm", "system"):  # 正常走模型（或元问题分流）不必刷屏；退化必须可见
                    await self.broadcast(note)
        finally:
            await self.broadcast({"type": "thinking", "active": False, "who": "dm"})

    async def on_suggest(self, name, ws):
        await self.send_to(
            ws, {"type": "thinking", "active": True, "who": "advisor", "label": "助手正在思考…"}
        )
        t_round = time.time()
        try:
            agent = self.advisor(name)
            res = await agent.suggest(
                AdvisorContext(state=self.state, player_name=name, chronicle=self.chronicle)
            )
            print(f"[RPGBar] 一轮 advisor {time.time()-t_round:5.1f}s"
                  f"（生成 {len(res.steps)} 次 · 工具 {'/'.join(res.used_tools) or '无'}"
                  f" · {len(res.data.get('options') or [])} 条建议）", flush=True)
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
            if note["source"] not in ("llm", "system"):
                await self.send_to(ws, note)
        finally:
            await self.send_to(ws, {"type": "thinking", "active": False, "who": "advisor"})

    @staticmethod
    def _agent_note(who: str, res) -> dict:
        """把「这一轮到底有没有真跑到模型」整理成一条可下发的事件。

        `AgentResult.source` 只有三种：llm（真的跑到了模型）/ system（入口确定性
        分流，如元问题直接回答）/ failed（这一轮没有产出，原因在 `error` 里）。
        2026-10-13 用户拍板取消脚本化兜底之后，不再有"用剧本拼一段旁白冒充模型"
        这条路 —— 所以非 llm/system 时界面必须把失败明说出来。
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

    # ---- 角色卡：玩家提交 ----

    async def on_character(self, name, attrs, ws):
        """玩家提交 / 修改自己的角色卡。

        三条规则：
        - 这个本不需要卡 ⇒ 直接回绝（界面本来也不该显示那一块）。
        - **等待中随便改**（还在准备，改来改去是正常的）；**开局后只能设一次** ——
          否则就能边打边加点，把 DC 当成橡皮泥捏。
        - 校验一律在**服务端**做（区间 / 基础值 / 点数总量）。前端那些加减号只是提示，
          改一下 JS 就能发任意数字过来。
        """
        async with self.lock:
            card = self.card_state.get("card")
            if not card or not card.get("required"):
                await self.send_to(
                    ws, {"type": "character_rejected", "errors": ["这个剧本不需要角色卡"]}
                )
                return
            p = self.state.players.get(name)
            if p is None:
                await self.send_to(
                    ws, {"type": "character_rejected", "errors": ["你不在这个房间里"]}
                )
                return
            if self.phase == PHASE_PLAYING and (p.character or {}).get("attrs"):
                await self.send_to(ws, {
                    "type": "character_rejected",
                    "errors": ["已经开局了，角色卡不能再改"],
                })
                return
            clean, errors = character_card.validate_allocation(card, attrs)
            if errors:
                await self.send_to(ws, {"type": "character_rejected", "errors": errors})
                return
            p.character = dict(p.character or {})
            p.character["attrs"] = clean
            await self.send_to(ws, {
                "type": "character_ok",
                "attrs": clean,
                "summary": character_card.summary(card, p.character),
            })
            # 队伍名单是所有人可见的：谁填了、填成什么样，都在 state 里
            await self.send_state()

    async def on_card_refresh(self, name, ws):
        """重试一次角色卡分析（只对「没分析出来」有意义；已有卡就原样重播）。"""
        async with self.lock:
            if self.card_state["status"] == "ready":
                await self.send_to(ws, {"type": "character_schema", **self.card_public()})
                return
            self._kick_card(force=True)


rooms = {}


def gen_code():
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))


app = FastAPI(title="RPGBar", version=__version__)

# llama.cpp 子进程随服务器退出收干净（引用归零才终止，见 llamacpp.shutdown_all）。
atexit.register(llamacpp_mod.shutdown_all)


@app.get("/api/version")
async def api_version():
    """版本信息，供客户端展示（版本号来自 server/__init__.py）。"""
    return {
        "name": "RPGBar",
        "version": __version__,
        "script": store.title if active["path"] is not None else None,
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


@app.get("/api/rooms")
async def api_rooms():
    """大厅：列出**公开**房间。

    私密房间不进这个列表（只能凭房间码 + 密码进），这是建私密房的全部意义。
    「等待中」的房间排在前面 —— 陌生人进得去的主要就是这些；已经开局的房间
    虽然也能中途加入，但先让人看到在等人的。
    """
    items = [
        room.lobby_info()
        for room in rooms.values()
        if room.visibility != VIS_PRIVATE and not room.closed
    ]
    items.sort(key=lambda r: (r["phase"] != PHASE_WAITING, -r["online"], -r["created_at"]))
    return {
        "rooms": items,
        "waiting": sum(1 for r in items if r["phase"] == PHASE_WAITING),
        "active_script": active["rel"],
        "active_title": store.title if active["path"] is not None else None,
        # 建房表单的缺省值：按当前「活动剧本」声明的人数算（剧本没写就是 1–4）
        "defaults": {
            "script": active["rel"],
            "script_title": store.title if active["path"] is not None else "请选择剧本",
            "players": dict(store.players) if active["path"] is not None else {"min": 1, "max": 4, "recommended": 1},
        },
    }


@app.get("/api/scripts")
async def api_scripts(include_internal: bool = False):
    """列出可用的剧本文件（scripts/ 目录下的 .json/.md/.txt）。"""
    return {
        "active": active["rel"],
        "active_title": store.title if active["path"] is not None else None,
        "dirs": [str(d) for d in script_dirs()],
        "scripts": list_scripts(include_internal=include_internal),
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
    joined = False  # 真的进了房间才在收尾时做「离开」处理（被拒的不算）
    try:
        first = json.loads(await ws.receive_text())
        if first.get("type") != "join":
            await ws.close()
            return
        name = (first.get("name") or "冒险者").strip()[:20] or "冒险者"
        character = first.get("character") or {}
        code = (first.get("room") or "").strip().upper()
        password = first.get("password")
        # 建房参数（可选）：房主在大厅点「创建房间」时带上。字段先过 lobby.clean_create，
        # 脏数据在 Room 构造里就被换成合法值了。
        create = first.get("create") if isinstance(first.get("create"), dict) else {}
        script_rel = create.get("script") if create.get("script") else first.get("script")
        room = rooms.get(code) if code else None
        created = room is None
        if created:
            # 两种情况都会走到这里：
            #  ① 大厅点「创建房间」—— 带了 create 参数；
            #  ② 输入了一个**还不存在**的房间码 —— 这是原有玩法（两个人各填同一个房号
            #     就能碰头），所以照旧允许，只是按默认设置建房（公开、1–4 人）。
            room_store = None
            inline_content = create.get("content") if isinstance(create.get("content"), str) else ""
            if inline_content.strip():
                try:
                    room_store = ScriptStore.from_content(
                        inline_content,
                        str(create.get("name") or "粘贴的剧本").strip()[:80],
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[RPGBar] 忽略建房请求里的剧本内容：{type(exc).__name__}: {exc}")
            if isinstance(script_rel, str) and script_rel.strip():
                p = _safe_script_path(script_rel)
                if p is not None:
                    try:
                        room_store = ScriptStore(p, label=_rel_of(p))
                    except Exception:  # noqa: BLE001
                        room_store = None
                if room_store is None:
                    print(f"[RPGBar] 忽略建房请求里无效的剧本：{script_rel!r}")
            if room_store is None and not (active["path"] is not None and not script_rel):
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": "创建房间前请选择剧本。服务器不会自动加载 demo 剧本。",
                }, ensure_ascii=False))
                await ws.close()
                return
            if not code:
                code = gen_code()
            # 没在 create 中传 path 时，才允许使用显式配置的活动剧本；默认没有活动剧本。
            room = Room(code, room_store or store, create=create,
                        script_rel=_rel_of(p) if (room_store is not None and script_rel) else (script_rel or ""))
            rooms[code] = room
            # 本局种子进日志、不进界面：要复现某一局的剧情就从这里取数
            # （同一个种子 + 同一串输入 → 同一条随机流 → 同一个故事）。
            print(
                f"[RPGBar] 新房「{code}」「{room.name}」剧本《{room.store.title}》"
                f"{'私密' if room.visibility == VIS_PRIVATE else '公开'}"
                f"/{room.max_players} 人 本局种子 {room.seed}"
                f"（DM {derive_seed(room.seed, 'dm')}"
                f" / 小助手 {derive_seed(room.seed, 'assistant')}）"
            )
        else:
            # 进已有的房间：先查密码与容量。密码/满了都**不再自动建房**（那是另一回事），
            # 直接回一条带机器可读 code 的 `join_denied`，界面据此显示并退回大厅。
            deny = room.join_denied(name, password)
            if deny:
                await ws.send_text(json.dumps({"type": "join_denied", **deny}, ensure_ascii=False))
                await ws.close()
                return
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
        joined = True
        await room.on_join(name, character, ws)

        busy = False  # 本连接是否有耗时任务在跑（同一玩家开两个标签页互不影响）
        while True:
            raw = await inbox.get()
            if raw is None:
                break
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "action":
                if not await room.require_playing(ws):
                    continue
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
                if not await room.require_playing(ws):
                    continue
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
            elif t == "roll":
                if not await room.require_playing(ws):
                    continue
                await room.on_roll(name)
            elif t == "configure":
                await room.on_configure(name, msg, ws)
            # ---- 角色卡（等待室里填，不需要过阶段门：开局前正是填卡的时机）----
            elif t == "character":
                await room.on_character(name, msg.get("attrs"), ws)
            elif t == "card_refresh":
                await room.on_card_refresh(name, ws)
            # ---- 大厅 / 等待室 ----
            elif t == "start":
                await room.on_start(name, ws)
            elif t == "room_settings":
                await room.on_room_settings(name, msg, ws)
            elif t == "kick":
                await room.on_kick(name, msg.get("target") or msg.get("name"), ws)
            elif t == "transfer":
                await room.on_transfer(name, msg.get("target") or msg.get("name"), ws)
            elif t == "close_room":
                await room.on_close_room(name, ws)
            elif t == "leave":
                break  # 主动离开走正常收尾（会广播「离开了队伍」）
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
        if joined and room is not None and name is not None:
            room.state.remove_player(name)
            ejected = name in room.ejected
            room.ejected.discard(name)
            room.clients = [(n, w) for n, w in room.clients if n != name]
            if room.closed:
                pass  # 房间已被房主解散，这里什么都不用做
            elif ejected:
                # 被房主请出去：服务端/房主已经宣布过了，别再广播一条「离开了队伍」
                if room.clients:
                    await room.broadcast_room()
            elif room.clients:
                await room.broadcast({"type": "system", "text": f"{name} 离开了队伍"})
                if room.owner == name:
                    # 房主走了、房间里还有人：房间按「有人连线就活着」继续存在，
                    # 所以房主身份必须有人接 —— 自动移交给**最早加入的在场玩家**
                    # （常见联网游戏的做法；房主也可以自己主动转让）。
                    heir = room.clients[0][0]
                    room.owner = heir
                    await room.broadcast({"type": "system", "text": f"房主已自动移交给 {heir}"})
                    await room.resync_context()
                else:
                    await room.send_state()
                await room.broadcast_room()
            else:
                rooms.pop(room.code, None)
                print(f"[RPGBar] 房间「{room.code}」最后一人离开，已回收")


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
