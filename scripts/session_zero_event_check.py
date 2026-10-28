"""Session Zero 初始化事实事件回归检查。"""
from pathlib import Path
import asyncio
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.rag import ScriptStore
from server.session_zero import prepare
from server.state_machine import GameState
from server.world import World


async def main():
    store = ScriptStore(Path("scripts/coc_sandbox.json"))
    path = Path(tempfile.gettempdir()) / "rpgbar_session_zero_check.sqlite"
    if path.exists():
        path.unlink()
    world = World(path, script="coc_sandbox.json")
    state = GameState(store, store.start_scene)
    ctx = SimpleNamespace(state=state, fact_events=[])
    await prepare(store, None, world, None, event_ctx=ctx)
    assert ctx.fact_events and ctx.fact_events[0]["type"] == "world_changed"
    assert ctx.fact_events[0]["target"] == "session_zero"
    world.close()
    path.unlink(missing_ok=True)
    print("session zero event: PASS")


if __name__ == "__main__":
    asyncio.run(main())
