"""两个 Agent：主机端 DM（大模型）与玩家私有小助手（小模型）。

两者共用 `base.Agent` 的工具调用循环，区别在**拿到的工具**：
- NarratorAgent 有写工具（move_to / set_flag / roll_check），能推进剧情；
- AdvisorAgent 只有读工具，结构上不可能改状态。

`describe_agents()` 供 `/api/version` 与界面展示当前两个槽位的接线状态。
"""
from .advisor import AdvisorAgent, AdvisorContext
from .base import Agent, AgentResult, Step, Tool
from .narrator import NarratorAgent, NarratorContext

__all__ = [
    "Agent",
    "AgentResult",
    "Step",
    "Tool",
    "NarratorAgent",
    "NarratorContext",
    "AdvisorAgent",
    "AdvisorContext",
    "describe_agents",
]


def describe_agents(dm_slot, advisor_slot) -> dict:
    """把两个模型槽位描述成可下发给前端的「状态」（绝不回传 api_key）。"""
    return {
        "dm": _slot_public(dm_slot, "主机 DM（大模型）"),
        "advisor": _slot_public(advisor_slot, "玩家小助手（小模型）"),
    }


def _slot_public(slot, label) -> dict:
    slot = slot or {}
    kind = (slot.get("kind") or "off").lower()
    has_key = bool((slot.get("api_key") or "").strip())
    ready = kind == "local" or (kind == "cloud" and has_key)
    return {
        "label": label,
        "kind": kind,
        "base_url": slot.get("base_url", ""),
        "model": slot.get("model", ""),
        "has_api_key": has_key,
        "ready": ready,
        "mode": "model" if ready else "scripted",
    }
