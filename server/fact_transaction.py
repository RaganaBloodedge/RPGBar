"""事实事件与 World 写入之间的补偿事务。

World SQLite 和 GameState 目前是两个存储边界，不能把它们伪装成同一个
数据库事务。这个助手把顺序和失败回滚统一起来：先登记尚未广播的事实，
World 写入成功后才把事件加入本轮产物；任一异常都撤销 GameState 事件。
调用方仍可在 mutation 内做剧本无关的 World 操作。
"""
from __future__ import annotations

from collections.abc import Callable


def commit_world_event(ctx, event_type: str, *, actor: str, target: str = "",
                       text: str = "", facts_added=None, facts_removed=None,
                       mutation: Callable[[], object],
                       world=None,
                       decorate: Callable[[dict, object], None] | None = None):
    """执行一次 GameState + World 的补偿事务。

    返回 ``(fact, result, error)``；成功时 error 为空，失败时 fact/result 为 None。
    ``mutation`` 返回 ``False``/``None`` 表示 World 拒绝写入。
    """
    try:
        fact = ctx.state.commit_event(
            event_type, actor=actor, target=target, text=text,
            facts_added=facts_added, facts_removed=facts_removed,
        )
    except Exception as exc:  # noqa: BLE001
        return None, None, f"事实提交失败：{type(exc).__name__}"
    snapshot = None
    try:
        if world is not None and hasattr(world, "transaction_snapshot"):
            snapshot = world.transaction_snapshot()
        result = mutation()
        if result is None or result is False or result == {}:
            raise ValueError("World 写入未确认成功")
    except Exception as exc:  # noqa: BLE001
        if snapshot is not None:
            try:
                world.restore_snapshot(snapshot)
            except Exception:
                pass
        ctx.state.rollback_event(fact["id"])
        return None, None, f"World 写入失败：{type(exc).__name__}"
    finally:
        if snapshot is not None:
            snapshot.close()
    if decorate is not None:
        decorate(fact, result)
    ctx.fact_events.append(fact)
    return fact, result, ""
