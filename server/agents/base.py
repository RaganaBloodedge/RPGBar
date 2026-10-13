"""标准 Agent 骨架：工具注册 + 工具调用循环 + 失败路径。

一个 Agent = 提示词 + 一组工具 + 循环。循环是标准写法：

    组装 messages（System 职责 + 次级 prompt（剧本）+ 本轮上下文）
    ┌─→ 调模型（带 tools 声明）
    │    ├─ 模型返回 tool_calls → 逐个执行 → 结果作为 role="tool" 回灌 ─┐
    │    └─ 模型返回终稿（或调用 finish 工具）→ 结束                     │
    └────────────────────────────────────────────────────────────────┘
    步数用尽 → 明确要求模型收敛为最终结果

提示词分两层，职责与内容解耦：

- **System prompt**（`system_prompt()`）：定义 Agent 自己的职责与边界，稳定不变。
- **次级 prompt**（`secondary_prompt()`）：排在 System prompt 之后的第二条 system 消息，
  由 `script_loader` 从剧本文件自动切片生成——换剧本只换这一层，Agent 人格与护栏不动。

只有两种结局：**跑到了模型**（`source="llm"`），或**这一轮没有产出**
（`source="failed"`，`error` 里带上原因）。

2026-10-13 用户拍板**取消脚本化兜底**：接不到模型、模型报错、或模型没给出可用
内容时，一律不再"用剧本原文拼一段旁白冒充 DM"。编出来的内容会把真实故障盖住——
玩家只看到"DM 敷衍了一句"，剧情其实原地没动。要玩就必须有一个模型（本地引擎或
云 API）；没有模型就把失败明确摆到界面上（见 `main.py` 的 `agent_note` 广播）。

另一条路是**单轮 JSON 协议**（`single_shot()`）：模型不支持工具调用时改用一次 JSON
完成同样的事。这**不是兜底** —— 它仍然真的调了模型，所以 `source` 依旧是 `llm`。

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
    source: str = "failed"  # llm / failed
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
    """Agent 基类。子类实现 system_prompt() / build_tools() / build_user_message()。

    提示词分两层：
    - `system_prompt()`：**职责**。我是谁、我能做什么、我的边界。稳定不变。
    - `secondary_prompt()`：**内容**。剧本世界观/语气/切片索引，随剧本切换而变，
      作为第二条 system 消息排在职责之后（优先级次于 System prompt，高于对话）。
    """

    name = "agent"
    max_steps = 6
    json_hint = _JSON_HINT

    def __init__(self, provider=None):
        self.provider = provider

    # ---- 子类实现 ----
    def system_prompt(self, ctx) -> str:
        raise NotImplementedError

    def secondary_prompt(self, ctx) -> str:
        """次级 prompt（剧本相关内容）。默认空。"""
        return ""

    def build_tools(self, ctx) -> list:
        raise NotImplementedError

    def build_user_message(self, ctx, user_input: str) -> str:
        raise NotImplementedError

    def init_messages(self, ctx, user_input: str) -> list:
        """组装起始消息：System（职责）→ System（次级 prompt）→ User（本轮上下文）。"""
        messages = [{"role": "system", "content": self.system_prompt(ctx)}]
        secondary = (self.secondary_prompt(ctx) or "").strip()
        if secondary:
            messages.append({"role": "system", "content": secondary})
        messages.append({"role": "user", "content": self.build_user_message(ctx, user_input)})
        return messages

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
            # 没有模型 → 显式失败。
            #
            # 这里以前回退到脚本化文案（"（DM）……你可以：……"）。2026-10-13 用户
            # 拍板取消这条兜底：编造出来的旁白会把真实故障盖住 —— 玩家分不清
            # "DM 敷衍了一句"和"模型根本没跑"，剧情原地没动却看不出来。宁可让
            # 失败摆到界面上。**要玩就必须有一个模型**（本地引擎，或云 API）。
            return AgentResult(
                text="",
                source="failed",
                stopped="no_provider",
                error="未配置模型",
            )

        by_name = {t.name: t for t in tools}
        messages = self.init_messages(ctx, user_input)
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
                    # 工具效果已经落进状态（骰子投过了、场景推进了），不回滚；
                    # 但**不补任何替代文本** —— 这一轮就是没有产出。
                    return AgentResult(
                        steps=steps,
                        source="failed",
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
                        source="llm",
                        stopped="no_tools" if no_tools else "final",
                    )
                # 既没工具调用也没内容 → 这一轮没有产出
                return AgentResult(
                    steps=steps,
                    source="failed",
                    stopped="no_tools" if no_tools else "final",
                    error="模型没有产出任何内容",
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
                        source="llm",
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
            return AgentResult(
                steps=steps,
                source="failed",
                stopped="max_steps",
                error="模型在工具调用上限内没有收敛出结果",
            )
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
        """模型不可用 / 这一轮没能产出 → **显式失败**。

        2026-10-13 用户拍板：不再回退到脚本化兜底。编出来的旁白会把真实故障盖住，
        玩家只看到"DM 敷衍了一句"，而剧情其实原地没动；失败摆到台面上，反而一眼
        就能看出问题在哪。要玩就必须有一个模型（本地引擎或云 API）。
        """
        return AgentResult(text="", steps=steps, source="failed", stopped="error", error=err)


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
