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
      "script_info":{...},"chunks":[...],"npcs":[...],
      "prompt":{"advisor":"...","dm":"...(仅房主)"}}  # 连入即同步（含本房间已有 NPC）
    {"type":"agent_status","agents":{...},"changed":["dm","advisor","assistant"]}   # 响应 configure
    {"type":"system","text":"..."}
    {"type":"narration","author":"DM","text":"..."}
    {"type":"dice","player":"...","skill":"...","dc":n,"roll":n,"success":bool|null,"flag":str|null}
    {"type":"suggestions","options":[...],"agent":{"source":"llm|scripted","tools":[...]}}
    {"type":"recap","recap":{"scene_path":[...],"flags":[...],"recent":[...],...}}  # 仅发给中途加入者
    {"type":"state","state":{...}}   # state.flag_details=[{id,label}]；state.npcs=[人物卡摘要]
    {"type":"error","message":"..."}

HTTP 接口：
    GET  /api/version          版本 / 当前剧本 / 两个 Agent 的接线状态
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
    describe_agents,
)
from .config import RESOURCE_DIR, USER_DIR, load_config
from .dice import roll
from .dm import DM
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
NPC_AUTO_PERSONA = bool((cfg.get("npc") or {}).get("auto_persona", True))

print(f"[RPGBar] 剧本：{store.title}（{len(store.scenes)} 场景 / {len(store.flag_ids)} 个 flag / {len(store.chunk_list)} 个切片）")
for w in store.warnings:
    print(f"[RPGBar] 剧本提示：{w}")
_agents = describe_agents(default_dm_slot, default_advisor_slot, default_assistant_slot)
for key in ("dm", "advisor", "assistant"):
    a = _agents.get(key)
    if not a:
        continue
    if a["ready"]:
        print(f"[RPGBar] {a['label']}：{a['kind']} · {a['base_url']} · {a['model']}")
    else:
        print(f"[RPGBar] {a['label']}：未接模型（留空），走脚本化兜底")
print("[RPGBar] 也可以在游戏内的「模型设置」里随时接入（云 API 或本地模型）。")


# ---- 模型槽位的清洗与合并 ----
ALLOWED_KINDS = ("off", "cloud", "local")


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
        self.state = GameState(self.store, self.store.start_scene)
        self.clients = []  # list[(name, websocket)]
        self.lock = asyncio.Lock()
        self.owner = None  # 房主（第一个加入的人）才能改 DM / 主机小助手模型
        self.dm_slot = dict(default_dm_slot)
        self.assistant_slot = dict(default_assistant_slot)
        self.advisors = {}  # name -> {"slot": dict, "agent": AdvisorAgent}
        self._narrator = None
        self._assistant = None
        # NPC 子系统：人物卡 + 隔离记忆（房间级，随房间钉住剧本）
        self.npc_enabled = NPC_AUTO_PERSONA
        self.npc = NpcService(self.store) if self.npc_enabled else None

    # ---- Agent 生命周期 ----
    def narrator(self) -> NarratorAgent:
        if self._narrator is None:
            self._narrator = NarratorAgent(self.store, build_provider(self.dm_slot), npc=self.npc)
        return self._narrator

    def assistant(self) -> AssistantAgent:
        """主机端小模型（NPC 人设 / 反应权重 / 摘要）。"""
        if self._assistant is None:
            agent = AssistantAgent(self.store, build_provider(self.assistant_slot))
            self._assistant = agent
            if self.npc is not None:
                self.npc.set_agent(agent)
        return self._assistant

    def advisor(self, name) -> AdvisorAgent:
        info = self.advisors.get(name)
        if info is None or info.get("agent") is None:
            slot = info["slot"] if info else dict(default_advisor_slot)
            info = {"slot": slot, "agent": AdvisorAgent(self.store, build_provider(slot))}
            self.advisors[name] = info
        return info["agent"]

    def agents_status(self, name=None) -> dict:
        status = describe_agents(self.dm_slot, self.advisor_slot_for(name), self.assistant_slot)
        status["owner"] = self.owner
        return status

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
        await self.broadcast({"type": "state", "state": snap})

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
                },
            )
            if not late:
                await self.broadcast({"type": "system", "text": f"{name} 加入了队伍"})
                await self.broadcast(
                    {"type": "narration", "author": "DM", "text": self.state.current().get("public_text", "")}
                )
            else:
                await self.broadcast({"type": "system", "text": f"{name} 中途加入了队伍"})
                intro = await DM(self.store, self.narrator().provider).introduce(self.state, name, character)
                await self.broadcast({"type": "narration", "author": "DM", "text": intro})
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
            await self.send_to(
                ws,
                {"type": "agent_status", "agents": self.agents_status(name), "changed": changed},
            )

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
            else:
                self.npc.remember(card.id, name, text, turn=self.state.turn, polarity=0.0)
        brief = self.npc.briefs_for(cards)
        return (brief + "\n" + "\n".join(notes)).strip()

    async def on_action(self, name, text):
        async with self.lock:
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
            await self.send_state()

    async def on_suggest(self, name, ws):
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
                },
            },
        )

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
        await room.on_join(name, character, ws)

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "action":
                await room.on_action(name, (msg.get("text") or "").strip())
            elif t == "suggest":
                await room.on_suggest(name, ws)
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
        if room is not None and name is not None:
            room.state.remove_player(name)
            room.clients = [(n, w) for n, w in room.clients if n != name]
            if room.clients:
                await room.broadcast({"type": "system", "text": f"{name} 离开了队伍"})
                await room.send_state()
            else:
                rooms.pop(room.code, None)


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
