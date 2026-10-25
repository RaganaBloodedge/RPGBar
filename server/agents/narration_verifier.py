"""风险触发式旁白审查 Agent。

只审查最终文字与服务端已确认事实的一致性，不拥有任何写状态工具。
普通回合由确定性护栏处理，只有检定、移动、越界和元叙事等高风险回合才调用模型。
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class Verification:
    accepted: bool = True
    issues: list[str] = None
    source: str = "skipped"
    error: str = ""

    def __post_init__(self):
        if self.issues is None:
            self.issues = []


class NarrationVerifierAgent:
    def __init__(self, provider=None):
        self.provider = provider

    @staticmethod
    def is_risky(ctx, text: str) -> bool:
        if getattr(ctx, "narration_parts", None):
            return True
        if getattr(ctx, "oob_verb", "") or getattr(ctx, "forced_check_id", ""):
            return True
        if any(getattr(ev, "get", lambda *_: None)("type") in {"dice", "system"}
               for ev in (getattr(ctx, "events", None) or [])):
            return True
        return any(x in (text or "") for x in ("宣布", "跳过", "已经拿到", "直接到"))

    @staticmethod
    def _prompt(facts: list[str], action: str, narration: str) -> str:
        return (
            "你是跑团 DM 的旁白审查器。只判断文字是否违反服务端已经确认的事实，"
            "不要重新裁定规则，不要修改状态。输出 JSON：accepted(bool)、issues(字符串数组)。\n"
            "已确认事实：" + json.dumps(facts, ensure_ascii=False) + "\n"
            f"玩家行动：{action}\n模型旁白：{narration}\n"
            "拒绝条件：把失败写成成功、让未获得物品出现、非法移动、泄露 DM 内幕、"
            "把玩家的宣布当成事实。纯粹的气氛和感官描写应接受。"
        )

    async def verify(self, ctx, action: str, narration: str) -> Verification:
        if self.provider is None or not self.is_risky(ctx, narration):
            return Verification()
        facts = [str(x) for x in (getattr(ctx, "narration_parts", None) or [])[-6:]]
        try:
            raw = await self.provider.chat([{"role": "user", "content": self._prompt(facts, action, narration)}])
            text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                a, b = text.find("{"), text.rfind("}")
                obj = json.loads(text[a:b + 1]) if a >= 0 and b > a else {}
            issues = obj.get("issues") if isinstance(obj.get("issues"), list) else []
            return Verification(bool(obj.get("accepted", True)), [str(x)[:180] for x in issues[:5]], "llm")
        except Exception as exc:  # noqa: BLE001
            # 审查器故障不能阻断游戏；确定性护栏仍然生效。
            return Verification(True, [], "failed", f"{type(exc).__name__}: {exc}")
