"""剧本驱动的行动裁定 Agent。

模型只负责把玩家自然语言归类并提出目标；最终检定、移动和状态写入仍由服务端完成。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class ActionJudgement:
    intent: str = "ambiguous"
    check_id: str = ""
    exit_scene: str = ""
    refusal: bool = False
    reason: str = ""
    source: str = "failed"
    error: str = ""


_ALLOWED = {"fact_question", "normal_action", "check", "move", "blocked", "meta", "ambiguous"}


class ActionJudgeAgent:
    """单轮 JSON Agent；不持有任何写状态工具。"""

    def __init__(self, provider=None):
        self.provider = provider

    @staticmethod
    def _prompt(scene: dict, action: str) -> str:
        view = {
            "title": scene.get("title"), "location": scene.get("location"),
            "public_text": scene.get("public_text"), "keywords": scene.get("keywords", []),
            "checks": [{"id": c.get("id"), "skill": c.get("skill"),
                        "keywords": c.get("keywords", [])} for c in scene.get("checks", [])],
            "exits": [{"to": e.get("to"), "label": e.get("label"),
                       "keywords": e.get("keywords", []), "condition": e.get("condition")} 
                      for e in scene.get("exits", [])],
        }
        return (
            "你是跑团行动裁定器。只根据当前场景判断玩家意图，不写旁白，不做骰子，不改状态。\n"
            "输出一个 JSON 对象：intent 必须是 fact_question/normal_action/check/move/blocked/meta/ambiguous；"
            "check 时给当前场景已有的 check id，move 时给当前场景已有出口 to，否则留空；"
            "blocked 表示玩家要求的事没有剧本机制或前提。不要因为动作听起来激烈就判 blocked，"
            "必须依据当前场景和出口/检定列表。\n"
            f"当前场景：{json.dumps(view, ensure_ascii=False)}\n玩家原话：{action}\n"
            '只输出：{"intent":"...","check_id":"","exit_scene":"","refusal":false,"reason":"..."}'
        )

    async def judge(self, scene: dict, action: str) -> ActionJudgement:
        if self.provider is None:
            return ActionJudgement(error="no_provider")
        try:
            raw = await self.provider.chat([{"role": "user", "content": self._prompt(scene, action)}])
            text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                a, b = text.find("{"), text.rfind("}")
                obj = json.loads(text[a:b + 1]) if a >= 0 and b > a else {}
            intent = str(obj.get("intent") or "ambiguous").strip()
            if intent not in _ALLOWED:
                intent = "ambiguous"
            return ActionJudgement(intent=intent, check_id=str(obj.get("check_id") or "").strip(),
                                   exit_scene=str(obj.get("exit_scene") or "").strip(),
                                   refusal=bool(obj.get("refusal")),
                                   reason=str(obj.get("reason") or "")[:240], source="llm")
        except Exception as exc:  # noqa: BLE001
            return ActionJudgement(error=f"{type(exc).__name__}: {exc}")

