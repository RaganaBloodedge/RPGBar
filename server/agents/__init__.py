"""Agent 层。

两条通道，两个引擎：

- **DM（主机端大模型）**：`NarratorAgent` 继承 `base.Agent` —— 标准工具调用循环，
  持有写工具（move_to / set_flag / roll_check），能推进剧情。
- **小助手（主机端 / 玩家端小模型）**：`ToolAgent`（见 `tool_agent.py`）—— 一个引擎挂多条
  固定 CoT 任务（`action_advice` / `persona` / `react_weights` / `summarize` ……），
  工具全只读，结构上改不了剧情。`AdvisorAgent` 就是它上面的 `action_advice` 任务。

`describe_agents()` 供 `/api/version` 与界面展示模型槽位的接线状态。
"""
from .advisor import AdvisorAgent, AdvisorContext
from .base import Agent, AgentResult, Step, Tool
from .narrator import NarratorAgent, NarratorContext
from .persona import AssistantAgent, SummaryCtx
from .tool_agent import Task, ToolAgent, parse_json

__all__ = [
    "Agent",
    "AgentResult",
    "Step",
    "Tool",
    "ToolAgent",
    "Task",
    "parse_json",
    "NarratorAgent",
    "NarratorContext",
    "AdvisorAgent",
    "AdvisorContext",
    "AssistantAgent",
    "SummaryCtx",
    "describe_agents",
]


def describe_agents(dm_slot, advisor_slot, assistant_slot=None) -> dict:
    """把模型槽位描述成可下发给前端的「状态」（绝不回传 api_key）。"""
    out = {
        "dm": _slot_public(dm_slot, "主机 DM（大模型）"),
        "advisor": _slot_public(advisor_slot, "玩家小助手（小模型）"),
    }
    if assistant_slot is not None:
        out["assistant"] = _slot_public(assistant_slot, "主机小助手 · NPC/摘要（小模型）")
    return out


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
