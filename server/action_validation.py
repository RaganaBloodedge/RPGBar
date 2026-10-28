"""把行动理解结果转换为可执行计划。

这里不判断玩家是否成功，只把当前剧本提供的检定和出口变成候选，并验证
ActionJudge 提出的目标是否存在。真正的骰子、移动和状态修改仍由工具和事件层完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActionPlan:
    intent: str = "ambiguous"
    check_id: str = ""
    exit_scene: str = ""
    check_candidates: list[dict] = field(default_factory=list)
    exit_candidates: list[dict] = field(default_factory=list)
    reason: str = ""


def candidates(scene: dict, text: str) -> tuple[list[dict], list[dict]]:
    """只使用当前场景切分数据生成候选，不维护剧本外关键词表。"""
    text = text or ""
    npc_names = {
        str(n.get("name") or "") for n in scene.get("npcs", []) or []
        if isinstance(n, dict)
    }
    checks = []
    for check in scene.get("checks", []) or []:
        hits = [str(k) for k in (check.get("keywords") or [])
                if str(k) in text and str(k) not in npc_names]
        if hits:
            checks.append({"id": check.get("id", ""), "skill": check.get("skill", ""),
                           "hits": hits})
    exits = []
    for exit_ in scene.get("exits", []) or []:
        # 出口目标的标题/地点由 ScriptStore 从切分结果补入当前场景视图。
        # 这让“回到古堡大门外”可以命中目标场景，而无需维护全局同义词表。
        terms = list(exit_.get("keywords") or [])
        terms.extend([exit_.get("label"), exit_.get("target_title"),
                      exit_.get("target_location")])
        terms.extend(exit_.get("target_keywords") or [])
        hits = [str(k) for k in terms if k and str(k) in text]
        if hits:
            exits.append({"to": exit_.get("to", ""), "label": exit_.get("label", ""),
                          "condition": exit_.get("condition"), "hits": hits})
    return checks, exits


def validate(scene: dict, text: str, judgement, fact_question: bool = False) -> ActionPlan:
    checks, exits = candidates(scene, text)
    plan = ActionPlan(check_candidates=checks, exit_candidates=exits)
    if fact_question:
        plan.reason = "事实回顾问题不触发当前场景机制"
        return plan
    valid_checks = {str(c.get("id")): c for c in scene.get("checks", []) or []}
    valid_exits = {str(e.get("to")): e for e in scene.get("exits", []) or []}
    confidence = float(getattr(judgement, "confidence", 0.0) or 0.0)
    if (getattr(judgement, "intent", "") == "check" and confidence >= 0.55
            and str(getattr(judgement, "check_id", "")) in valid_checks):
        plan.intent = "check"
        plan.check_id = str(judgement.check_id)
        return plan
    if (getattr(judgement, "intent", "") == "move" and confidence >= 0.55
            and str(getattr(judgement, "exit_scene", "")) in valid_exits):
        plan.intent = "move"
        plan.exit_scene = str(judgement.exit_scene)
        return plan
    if len(checks) == 1:
        plan.intent = "check"
        plan.check_id = str(checks[0].get("id") or "")
        return plan
    if len(exits) == 1:
        plan.intent = "move"
        plan.exit_scene = str(exits[0].get("to") or "")
        return plan
    judged_intent = getattr(judgement, "intent", "ambiguous") or "ambiguous"
    if judged_intent == "blocked":
        # 拒绝是高风险裁定：低置信度只能进入歧义分支，不能把玩家自由行动
        # 永久锁死。下一层 Narrator 仍可据当前现场给出澄清或普通回应。
        plan.intent = "blocked" if confidence >= 0.70 else "ambiguous"
    else:
        plan.intent = judged_intent
    return plan
