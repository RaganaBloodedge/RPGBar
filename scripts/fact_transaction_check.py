"""补偿事实事务的最小回归检查。"""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.fact_transaction import commit_world_event
from server.rag import ScriptStore
from server.state_machine import GameState


def main():
    store = ScriptStore(Path("scripts/sample_script.json"))
    state = GameState(store, next(iter(store.scenes)))
    ctx = SimpleNamespace(state=state, fact_events=[])

    fact, result, error = commit_world_event(
        ctx, "entity_declared", actor="测试", target="不存在的物件",
        mutation=lambda: (_ for _ in ()).throw(RuntimeError("模拟 World 故障")),
    )
    assert fact is None and result is None and error
    assert not state.fact_events and not ctx.fact_events

    fact, result, error = commit_world_event(
        ctx, "entity_declared", actor="测试", target="铜钥匙",
        mutation=lambda: {"name": "铜钥匙"},
    )
    assert fact and result["name"] == "铜钥匙" and not error
    assert len(state.fact_events) == 1 and len(ctx.fact_events) == 1
    print("fact transaction: PASS")


if __name__ == "__main__":
    main()
