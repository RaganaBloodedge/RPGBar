"""FastAPI + WebSocket 多人房间服务。

协议（JSON 文本帧）：
  客户端 → 服务端：
    {"type":"join","name":"...","character":{...},"room":"ABC12"(可空)}
    {"type":"action","text":"..."}
    {"type":"suggest"}
    {"type":"roll"}
  服务端 → 客户端：
    {"type":"welcome","room":"...","you":"..."}
    {"type":"system","text":"..."}
    {"type":"narration","author":"DM","text":"..."}
    {"type":"dice","player":"...","skill":"...","dc":n,"roll":n,"success":bool|null,"flag":str|null}
    {"type":"suggestions","options":[...]}
    {"type":"state","state":{...}}
    {"type":"error","message":"..."}
"""
import asyncio
import json
import secrets
import string
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, load_config
from .dice import roll
from .dm import DM
from .llm import build_provider
from .rag import ScriptStore
from .state_machine import GameState

cfg = load_config()
store = ScriptStore(BASE_DIR / "scripts" / "sample_script.json")
provider = build_provider(cfg)

if provider is None:
    print("[RPGBar] 未配置 LLM API key，DM 走脚本化兜底（玩法完整可玩）。")
else:
    print(f"[RPGBar] LLM provider: {provider.name} ({cfg['llm']['model']})")


class Room:
    def __init__(self, code):
        self.code = code
        self.state = GameState(store, store.start_scene)
        self.dm = DM(store, provider)
        self.clients = []  # list[(name, websocket)]
        self.lock = asyncio.Lock()

    async def broadcast(self, msg):
        for _, ws in list(self.clients):
            try:
                await ws.send_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                pass

    async def send_state(self):
        await self.broadcast({"type": "state", "state": self.state.snapshot()})

    async def on_join(self, name, character, ws):
        self.state.add_player(name, character)
        self.clients.append((name, ws))
        await ws.send_text(json.dumps({"type": "welcome", "room": self.code, "you": name}, ensure_ascii=False))
        await self.broadcast({"type": "system", "text": f"{name} 加入了队伍"})
        await self.broadcast({"type": "narration", "author": "DM", "text": self.state.current().get("public_text", "")})
        await self.send_state()

    async def on_action(self, name, text):
        async with self.lock:
            for ev in await self.dm.handle_action(self.state, name, text):
                await self.broadcast(ev)
            await self.send_state()

    async def on_suggest(self, ws):
        opts = await self.dm.suggest(self.state)
        await ws.send_text(json.dumps({"type": "suggestions", "options": opts}, ensure_ascii=False))

    async def on_roll(self, name):
        await self.broadcast(
            {"type": "dice", "player": name, "skill": "d20", "dc": None, "roll": roll(20), "success": None, "flag": None}
        )


rooms = {}


def gen_code():
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))


app = FastAPI(title="RPGBar")


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
        room = rooms.setdefault(code, Room(code))
        await room.on_join(name, character, ws)

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "action":
                await room.on_action(name, (msg.get("text") or "").strip())
            elif t == "suggest":
                await room.on_suggest(ws)
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


WEB_DIR = BASE_DIR / "web"
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
    import os

    import uvicorn

    s = cfg["server"]
    port = int(os.environ.get("PORT", s["port"]))
    host = "0.0.0.0" if os.environ.get("PORT") else s["host"]
    print(f"[RPGBar] 服务器监听 {host}:{port}")
    if host in ("0.0.0.0", "::"):
        print(f"[RPGBar] 本机访问: http://127.0.0.1:{port}")
        lan = _lan_ip()
        if lan:
            print(f"[RPGBar] 局域网访问（发给朋友）: http://{lan}:{port}")
    uvicorn.run("server.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
