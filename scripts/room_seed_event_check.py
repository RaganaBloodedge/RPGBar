"""验证固定剧本房间的 World 种子也进入事实事件链。"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import main as app
from server.rag import ScriptStore
from server.world import World


def main() -> int:
    code = "SEED" + uuid.uuid4().hex[:6].upper()
    room = app.Room(code, script_store=ScriptStore("scripts/sample_script.json"),
                    create={"visibility": "private", "max_players": 1})
    try:
        events = room._bootstrap_events
        assert any(e.get("type") == "world_changed"
                   and e.get("target") == "room_seed" for e in events), events
        assert any(e.get("target") == "room_seed" for e in room.state.fact_events), events
        assert room.world is not None and room.world.scene()["id"] == room.store.start_scene
        print("room seed fact: PASS")
    finally:
        if room.world is not None:
            path = room.world.path
            room.world.close()
            path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
