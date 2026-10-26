"""Verify script-neutral fact commits and Chronicle projection."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.chronicle import Chronicle
from server.rag import ScriptStore
from server.state_machine import GameState


def main() -> int:
    store = ScriptStore(Path(__file__).with_name("sample_script.json"))
    state = GameState(store, store.start_scene)
    initial = state.snapshot()
    try:
        state.commit_event("scene_entered", actor="玩家", target="secret_room")
        raise AssertionError("invalid exit was accepted")
    except ValueError:
        assert state.snapshot() == initial

    move = state.commit_event("scene_entered", actor="玩家", target="hall")
    assert move["scene_before"] == "gate" and move["scene_after"] == "hall"
    assert state.current_scene == "hall"
    assert move["source"] == "server"

    check = state.commit_event("check_resolved", actor="玩家",
                               target="search_tapestry", success=True)
    assert "found_secret_door" in check["facts_added"]
    assert "found_secret_door" in state.flags
    world_event = state.commit_event("time_advanced", actor="玩家", target="10",
                                     text="过去 10 分钟", facts_added=["world_time:1:10"])
    assert world_event["type"] == "time_advanced"
    assert world_event["facts_added"] == ["world_time:1:10"]
    state.commit_event("scene_entered", actor="玩家", target="secret_room")
    assert state.current_scene == "secret_room"

    with tempfile.TemporaryDirectory() as temp:
        archive = Chronicle(Path(temp) / "events.jsonl")
        archive.add("调查挂毯成功", entry=check["id"], kind="event",
                    turn=check["turn"], scene=check["scene_after"],
                    actors=[check["actor"]], event_type=check["type"],
                    facts_added=check["facts_added"],
                    scene_before=check["scene_before"],
                    scene_after=check["scene_after"], target=check["target"],
                    success=check["success"])
        found = Chronicle(Path(temp) / "events.jsonl").search("挂毯", kinds=["event"])
        assert len(found) == 1 and found[0]["facts_added"] == ["found_secret_door"]
        assert found[0]["success"] is True
    print("fact event checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
