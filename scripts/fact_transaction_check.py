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

    # 场景移动和时间推进也必须走同一个补偿边界：先写 World 再失败时，
    # GameState 与 SQLite 都应回到提交前状态。
    store.scenes["txn_scene"] = {
        "id": "txn_scene", "title": "事务场景", "location": "事务地点",
        "public_text": "", "dm_notes": "", "npcs": [], "keywords": [],
        "flags_required": [], "flags_set": [], "checks": [], "exits": [],
    }
    ctx3 = SimpleNamespace(state=GameState(store, "gate"), fact_events=[])
    world_before_scene = world.scene()["id"]
    def move_then_fail():
        world.add_scene("txn_scene", "事务场景", "事务地点", "")
        world.enter("txn_scene")
        raise RuntimeError("模拟移动后故障")
    _fact, _result, error = commit_world_event(
        ctx3, "scene_entered", actor="测试", target="txn_scene", world=world,
        mutation=move_then_fail,
    )
    assert error and ctx3.state.current_scene == "gate"
    assert world.scene()["id"] == world_before_scene

    ctx4 = SimpleNamespace(state=GameState(store, "gate"), fact_events=[])
    world_before_turn = world.get("turn", "0")
    def tick_then_fail():
        world.bump_turn()
        raise RuntimeError("模拟时间写入后故障")
    _fact, _result, error = commit_world_event(
        ctx4, "time_advanced", actor="测试", target="turn", world=world,
        mutation=tick_then_fail,
    )
    assert error and world.get("turn", "0") == world_before_turn
    world.close()
    world_path.unlink(missing_ok=True)
    print("fact transaction: PASS")


if __name__ == "__main__":
    main()
