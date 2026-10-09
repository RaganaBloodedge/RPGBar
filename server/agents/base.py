"""标准 Agent 骨架：工具注册 + 工具调用循环 + 降级路径。

一个 Agent = 系统提示词 + 一组工具 + 循环。循环是标准写法：

    组装 messages（system + 上下文）
    ┌─→ 调模型（带 tools 声明）
    │    ├─ 模型返回 tool_calls → 逐个执行 → 结果作为 role="tool" 回灌 ─┐
    │    └─ 模型返回终稿（或调用 finish 工具）→ 结束                     │
    └────────────────────────────────────────────────────────────────┘
    步数用尽 → 明确要求模型收敛为最终结果

两条降级路径（保证"没接模型也能玩"）：

1. **没有 provider**（kind=off，即用户留空 API）→ 走 `fallback_text()`，
   由各 Agent 用剧本原文拼出确定性输出。
2. **模型不支持工具调用**（小模型/老服务常见）→ 收到 `ToolCallingUnsupported`
   后切到 `single_shot()`，用单轮 JSON 协议完成同样的事。

每次工具调用都会记进 `AgentResult.steps`，便于在界面上展示、也便于测试断言。
"""
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..llm import ToolCallingUnsupported


@dataclass
class Tool:
    """一个可被模型调用的工具。read_only=False 表示会改游戏状态。"""

    name: str
    description: str
    parameters: dict
    handler: Callable[..., Any]
    read_only: bool = True

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Step:
    """一次工具调用（trace 用）。"""

    tool: str
    args: dict
    result: Any


@dataclass
class AgentResult:
    text: str = ""
    data: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    source: str = "scripted"  # llm / scripted / degraded
    stopped: str = "final"  # final / max_steps / no_provider / no_tools / error
    error: str = ""

    @property
    def used_tools(self) -> list:
        return [s.tool for s in self.steps]


def _obj(props: dict | None = None, required: list | None = None) -> dict:
    return {
        "type": "object",
        "properties": props or {},
        "required": required or [],
    }


def str_prop(desc: str) -> dict:
    return {"type": "string", "description": desc}


def int_prop(desc: str, default: int | None = None) -> dict:
    p = {"type": "integer", "description": desc}
    if default is not None:
        p["default"] = default
    return p


_JSON_HINT = {
    "role": "user",
    "content": "你无法使用工具。请直接输出最终 JSON 结果（不要输出任何解释文字）。",
}


class Agent:
    """Agent 基类。子类实现 system_prompt() / build_tools() / build_user_message()。"""

    name = "agent"
    max_steps = 6
    json_hint = _JSON_HINT

    def __init__(self, provider=None):
        self.provider = provider

    # ---- 子类实现 ----
    def system_prompt(self, ctx) -> str:
        raise NotImplementedError

    def build_tools(self, ctx) -> list:
        raise NotImplementedError

    def build_user_message(self, ctx, user_input: str) -> str:
        raise NotImplementedError

    def fallback_text(self, ctx, user_input: str) -> str:
        """无 provider 时的确定性输出。"""
        return ""

    async def single_shot(self, ctx, user_input: str, tools: list) -> str:
        """模型不支持工具调用时的单轮 JSON 协议（子类可覆写）。"""
        return ""

    def parse_final(self, text: str, ctx) -> Any:
        """把模型终稿解析成结构化结果（子类可覆写）。"""
        return None

    # ---- 循环 ----
    async def run(self, ctx, user_input: str = "") -> AgentResult:
        tools = self.build_tools(ctx)
        if self.provider is None:
            return AgentResult(
                text=self.fallback_text(ctx, user_input),
                source="scripted",
                stopped="no_provider",
            )

        by_name = {t.name: t for t in tools}
        messages = [
            {"role": "system", "content": self.system_prompt(ctx)},
            {"role": "user", "content": self.build_user_message(ctx, user_input)},
        ]
        schemas = [t.schema() for t in tools]
        steps: list[Step] = []
        no_tools = False

        for _ in range(self.max_steps):
            try:
                if no_tools:
                    reply = {"content": await self._json_chat(messages), "tool_calls": []}
                else:
                    reply = await self.provider.chat_with_tools(messages, schemas)
            except ToolCallingUnsupported:
                no_tools = True
                try:
                    reply = {"content": await self._json_chat(messages), "tool_calls": []}
                except Exception as e:  # noqa: BLE001
                    return self._degraded(ctx, user_input, steps, str(e))
            except Exception as e:  # noqa: BLE001
                if steps:
                    # 已经动过手了，之前的工具效果保留；补一段兜底旁白
                    return AgentResult(
                        text=self.fallback_text(ctx, user_input),
                        steps=steps,
                        source="degraded",
                        stopped="error",
                        error=str(e),
                    )
                return self._degraded(ctx, user_input, steps, str(e))

            calls = reply.get("tool_calls") or []
            if not calls:
                text = (reply.get("content") or "").strip()
                if text:
                    parsed = self.parse_final(text, ctx)
                    if isinstance(parsed, dict):
                        # 单轮 JSON 协议下，真正要展示的是里面的旁白
                        text = (parsed.get("narration") or parsed.get("text") or text).strip()
                    return AgentResult(
                        text=text,
                        data=parsed if isinstance(parsed, dict) else {},
                        steps=steps,
                        source="degraded" if no_tools else "llm",
                        stopped="no_tools" if no_tools else "final",
                    )
                # 既没工具调用也没内容 → 兜底
                return AgentResult(
                    text=self.fallback_text(ctx, user_input),
                    steps=steps,
                    source="degraded",
                    stopped="no_tools" if no_tools else "final",
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.get("content") or "",
                    "tool_calls": calls,
                }
            )
            for call in calls:
                fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                tool_name = fn.get("name") or ""
                args = _parse_args(fn.get("arguments"))
                tool = by_name.get(tool_name)
                if tool is None:
                    result = {"error": f"未知工具：{tool_name}"}
                else:
                    try:
                        result = tool.handler(ctx, **args)
                        if asyncio.iscoroutine(result):
                            result = await result
                    except Exception as e:  # noqa: BLE001
                        result = {"error": f"{type(e).__name__}: {e}"}
                    steps.append(Step(tool=tool_name, args=args, result=result))

                # finish 工具：模型显式结束
                if tool is not None and tool.name == "finish":
                    data = result if isinstance(result, dict) else {}
                    return AgentResult(
                        text=data.get("narration") or data.get("text") or "",
                        data=data,
                        steps=steps,
                        source="degraded" if no_tools else "llm",
                        stopped="no_tools" if no_tools else "final",
                    )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or tool_name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        # 步数用尽：要求收敛
        messages.append(
            {
                "role": "user",
                "content": "已到达工具调用上限。请不要再调用任何工具，直接给出最终结果。",
            }
        )
        try:
            reply = await self.provider.chat_with_tools(messages, [])
            text = (reply.get("content") or "").strip()
        except Exception as e:  # noqa: BLE001
            return self._degraded(ctx, user_input, steps, str(e))
        if not text:
            text = self.fallback_text(ctx, user_input)
        parsed = self.parse_final(text, ctx)
        return AgentResult(
            text=text,
            data=parsed if isinstance(parsed, dict) else {},
            steps=steps,
            source="llm",
            stopped="max_steps",
        )

    async def _json_chat(self, messages) -> str:
        msgs = [dict(m) for m in messages if m.get("role") in ("system", "user")]
        msgs.append(self.json_hint)
        return await self.provider.chat(msgs, json_mode=True)

    def _degraded(self, ctx, user_input, steps, err) -> AgentResult:
        return AgentResult(
            text=self.fallback_text(ctx, user_input),
            steps=steps,
            source="scripted",
            stopped="error",
            error=err,
        )


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
