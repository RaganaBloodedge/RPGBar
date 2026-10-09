"""FastAPI + WebSocket 多人房间服务。

协议（JSON 文本帧）：
  客户端 → 服务端：
    {"type":"join","name":"...","character":{...},"room":"ABC12"(可空)}
    {"type":"action","text":"..."}
    {"type":"suggest"}
    {"type":"roll"}
    {"type":"configure","dm":{...}(可空),"advisor":{...}(可空)}   # 运行期接模型；dm 仅房主可改
       槽位字段：kind(off|cloud|local) + base_url + model + temperature + api_key(省略=保持原值)
  服务端 → 客户端：
    {"type":"welcome","room":"...","you":"...","late":bool,"script":"剧本名","agents":{...}}
    {"type":"agent_status","agents":{...},"changed":["dm","advisor"]}   # 响应 configure
    {"type":"system","text":"..."}
    {"type":"narration","author":"DM","text":"..."}
    {"type":"dice","player":"...","skill":"...","dc":n,"roll":n,"success":bool|null,"flag":str|null}
    {"type":"suggestions","options":[...],"agent":{"source":"llm|scripted","tools":[...]}}
    {"type":"recap","recap":{"scene_path":[...],"flags":[...],"recent":[...],...}}  # 仅发给中途加入者
    {"type":"state","state":{...}}   # state.flag_details = [{"id":..,"label":..}]
    {"type":"error","message":"..."}

HTTP 接口：
    GET  /api/version          版本 / 当前剧本 / 两个 Agent 的接线状态
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
from .agents import AdvisorAgent, AdvisorContext, NarratorAgent, NarratorContext, describe_agents
from .config import RESOURCE_DIR, USER_DIR, load_config
from .dice import roll
from .dm import DM
from .llm import ToolCallingUnsupported, build_provider
from .rag import ScriptStore
from .state_machine import GameState

cfg = load_config()


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

print(f"[RPGBar] 剧本：{store.title}（{len(store.scenes)} 场景 / {len(store.flag_ids)} 个 flag / {len(store.chunk_list)} 个切片）")
for w in store.warnings:
    print(f"[RPGBar] 剧本提示：{w}")
_agents = describe_agents(default_dm_slot, default_advisor_slot)
for key in ("dm", "advisor"):
    a = _agents[key]
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


class Room:
    def __init__(self, code, script_store=None):
        self.code = code
        # 房间在创建时「钉住」当时的活动剧本：之后服务端换剧本，进行中的对局不受影响。
        self.store = script_store or store
        self.state = GameState(self.store, self.store.start_scene)
        self.clients = []  # list[(name, websocket)]
        self.lock = asyncio.Lock()
        self.owner = None  # 房主（第一个加入的人）才能改 DM 模型
        self.dm_slot = dict(default_dm_slot)
        self.advisors = {}  # name -> {"slot": dict, "agent": AdvisorAgent}
        self._narrator = None

    # ---- Agent 生命周期 ----
    def narrator(self) -> NarratorAgent:
        if self._narrator is None:
            self._narrator = NarratorAgent(self.store, build_provider(self.dm_slot))
        return self._narrator

    def advisor(self, name) -> AdvisorAgent:
        info = self.advisors.get(name)
        if info is None or info.get("agent") is None:
            slot = info["slot"] if info else dict(default_advisor_slot)
            info = {"slot": slot, "agent": AdvisorAgent(self.store, build_provider(slot))}
            self.advisors[name] = info
        return info["agent"]

    def agents_status(self, name=None) -> dict:
        status = describe_agents(self.dm_slot, self.advisor_slot_for(name))
        status["owner"] = self.owner
        return status

    def advisor_slot_for(self, name):
        info = self.advisors.get(name)
        return info["slot"] if info else dict(default_advisor_slot)

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
        await self.broadcast({"type": "state", "state": self.state.snapshot()})

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
        """运行期接入模型：dm 仅房主可改；advisor 每个玩家管自己的。"""
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

    async def on_action(self, name, text):
        async with self.lock:
            ctx = NarratorContext(state=self.state, player_name=name)
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
        "agents": describe_agents(default_dm_slot, default_advisor_slot),
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
        if not code:
            code = gen_code()
        room = rooms.setdefault(code, Room(code, store))
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


def _lan_ip():
    """获取本机局域网 IP，用于告知朋友连接地址。"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def main():
    import uvicorn

    s = cfg["server"]
    port = int(os.environ.get("PORT", s["port"]))
    host = "0.0.0.0" if os.environ.get("PORT") else s["host"]
    print(f"[RPGBar] v{__version__} 服务器监听 {host}:{port}")
    if host in ("0.0.0.0", "::"):
        print(f"[RPGBar] 本机访问: http://127.0.0.1:{port}")
        lan = _lan_ip()
        if lan:
            print(f"[RPGBar] 局域网访问（发给朋友）: http://{lan}:{port}")
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
