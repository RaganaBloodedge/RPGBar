"""补偿事实事务的最小回归检查。"""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.fact_transaction import commit_world_event
from server.rag import ScriptStore
from server.state_machine import GameState
from server.world import World


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

    world_path = Path(tempfile.gettempdir()) / "rpgbar_fact_transaction.sqlite"
    world_path.unlink(missing_ok=True)
    world = World(world_path, script="sample_script.json")
    world.add_scene("gate", "大门", "", "")
    world.enter("gate")
    before = world.present()
    ctx2 = SimpleNamespace(state=GameState(store, "gate"), fact_events=[])
    def mutate_then_fail():
        world.declare("临时实体")
        raise RuntimeError("模拟写入后故障")
    _fact, _result, error = commit_world_event(
        ctx2, "entity_declared", actor="测试", target="临时实体", world=world,
        mutation=mutate_then_fail,
    )
    assert error and world.present() == before
    world.close()
    world_path.unlink(missing_ok=True)
    print("fact transaction: PASS")


if __name__ == "__main__":
    main()
