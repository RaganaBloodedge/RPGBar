"""通用「小模型工具代理」：一个引擎，挂多条固定 CoT 任务。

为什么要有它
------------
主机端与玩家端的**小助手本质是同一个东西**：一个跑在本地/小模型上的工具代理。
它要干的活（给行动建议、生成人物小传、算 NPC 反应权重、滚动摘要……）都是**固定流程**，
只是职责不同。于是统一成「一个引擎 + 若干任务模板」：

- 每个任务 = 一条固定 CoT：职责 system prompt + 次级 prompt（剧本内容）+ 输出 schema + 校验/兜底。
- 任务的**执行方式**分两类：
  - 带工具的（如 action_advice）→ 走标准工具循环（工具全只读，结构上改不了剧情）；
  - 不带工具的（如 persona / react_weights）→ 单轮 JSON，一次调用拿到结构化结果。

护栏放在**校验器**（validate）里，而不是靠提示词祈求：字段补齐、范围裁剪、权重归一化，
模型再怎么乱来，落到游戏里的数据都是合法的——跟「护栏做在工具体里」是同一条原则。
"""
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..llm import ToolCallingUnsupported
from .base import AgentResult, Step, Tool, _obj, int_prop, str_prop  # noqa: F401  (re-export 给各任务用)

__all__ = ["Task", "ToolAgent", "parse_json", "Tool", "AgentResult", "Step", "_obj", "int_prop", "str_prop"]


def parse_json(raw) -> dict:
    """从模型输出里抠出一个 JSON 对象（容忍 ``` 包裹与前后废话）。"""
    import re

    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {}
        try:
            val = json.loads(m.group(0))
            return val if isinstance(val, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


def _resolve(value, agent, ctx):
    """任务字段可以是常量，也可以是 (agent, ctx) -> 值 的函数。"""
    return value(agent, ctx) if callable(value) else value


@dataclass
class Task:
    """一条固定 CoT 任务。

    字段里凡是 Callable 的，签名统一为 (agent, ctx) -> 值：
    - system：职责提示词（第一条 system 消息）
    - secondary：次级提示词（第二条 system 消息，通常是剧本内容；返回空串则不加）
    - user：本轮上下文（user 消息）
    - tools：返回 Tool 列表；非空则走工具循环，否则走单轮 JSON
    - json_hint：追加的「请只输出这个 JSON」提示（单轮 JSON 用）
    - fallback：无模型 / 解析失败时的确定性兜底数据
    - validate：护栏。把模型给的 raw dict 清洗成合法数据；返回空 dict 视为不合格 → 走兜底
    - parse_final：工具循环结束、模型只给了文本时，从中解析结构化数据
    """

    name: str
    system: Any
    secondary: Any = None
    user: Any = ""
    tools: Any = None
    json_hint: str = ""
    fallback: Callable[[Any, Any], dict] | None = None
    validate: Callable[[Any, dict, Any], dict] | None = None
    parse_final: Callable[[Any, str, Any], dict] | None = None
    max_steps: int = 4
    label: str = ""  # 给界面/日志看的可读名

    def run_label(self) -> str:
        return self.label or self.name


class ToolAgent:
    """小模型工具代理：同一套 provider，挂载多条固定任务。

    子类在 `__init__` 里往 `self.tasks` 注册任务即可；执行入口一律是 `run_task(name, ctx)`。
    """

    name = "assistant"
    default_task = ""  # 子类可指定「默认任务」，供 system_prompt()/secondary_prompt() 这类旧式钩子使用

    def __init__(self, store=None, provider=None):
        self.store = store
        self.provider = provider
        self.tasks: dict[str, Task] = {}

    # ---- 任务选择 ----
    def pick_task(self, name: str | None = None) -> Task | None:
        if name and name in self.tasks:
            return self.tasks[name]
        if self.default_task and self.default_task in self.tasks:
            return self.tasks[self.default_task]
        return next(iter(self.tasks.values()), None)

    # ---- 旧式提示词钩子（保持与 base.Agent 同样的两层语义）----
    def system_prompt(self, ctx, task: str | None = None) -> str:
        t = self.pick_task(task)
        return str(_resolve(t.system, self, ctx) or "") if t else ""

    def secondary_prompt(self, ctx, task: str | None = None) -> str:
        t = self.pick_task(task)
        return str(_resolve(t.secondary, self, ctx) or "") if t else ""

    def build_user_message(self, ctx, user_input: str = "", task: str | None = None) -> str:
        t = self.pick_task(task)
        return str(_resolve(t.user, self, ctx) or "") if t else ""

    # ---- 注册 ----
    def register(self, task: Task):
        self.tasks[task.name] = task
        return task

    def has_task(self, name: str) -> bool:
        return name in self.tasks

    # ---- 对外入口 ----
    async def run_task(self, name: str, ctx) -> AgentResult:
        task = self.tasks.get(name)
        if task is None:
            return AgentResult(data={}, source="scripted", stopped="error", error=f"未知任务：{name}")
        if self.provider is None:
            return AgentResult(data=self._fallback(task, ctx), source="scripted", stopped="no_provider")
        tools = list(_resolve(task.tools, self, ctx) or [])
        if tools:
            return await self._run_tools(task, ctx, tools)
        return await self._run_single(task, ctx)

    # ---- 消息装配：System（职责）→ System（次级 prompt）→ User（本轮上下文）----
    def build_messages(self, task: Task, ctx) -> list:
        messages = [{"role": "system", "content": str(_resolve(task.system, self, ctx) or "")}]
        secondary = str(_resolve(task.secondary, self, ctx) or "").strip()
        if secondary:
            messages.append({"role": "system", "content": secondary})
        messages.append({"role": "user", "content": str(_resolve(task.user, self, ctx) or "")})
        return messages

    def _fallback(self, task: Task, ctx) -> dict:
        if task.fallback is None:
            return {}
        try:
            return task.fallback(self, ctx) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _validate(self, task: Task, raw: dict, ctx) -> dict:
        if not isinstance(raw, dict) or not raw:
            return {}
        if task.validate is None:
            return raw
        try:
            out = task.validate(self, raw, ctx)
        except Exception:  # noqa: BLE001
            return {}
        return out if isinstance(out, dict) else {}

    # ---- 单轮 JSON（无工具任务）----
    async def _run_single(self, task: Task, ctx) -> AgentResult:
        messages = self.build_messages(task, ctx)
        hint = task.json_hint
        if hint:
            messages.append({"role": "user", "content": hint})
        try:
            raw = await self.provider.chat(messages, json_mode=True)
        except Exception as e:  # noqa: BLE001
            return AgentResult(
                data=self._fallback(task, ctx), source="scripted", stopped="error", error=str(e)
            )
        data = self._validate(task, parse_json(raw), ctx)
        if not data:
            return AgentResult(data=self._fallback(task, ctx), source="degraded", stopped="final")
        return AgentResult(data=data, source="llm", stopped="final")

    # ---- 工具循环（带工具任务，与 base.Agent 同一套写法）----
    async def _run_tools(self, task: Task, ctx, tools: list) -> AgentResult:
        by_name = {t.name: t for t in tools}
        schemas = [t.schema() for t in tools]
        messages = self.build_messages(task, ctx)
        steps: list[Step] = []
        no_tools = False

        for _ in range(task.max_steps):
            try:
                if no_tools:
                    reply = {"content": await self._json_chat(task, messages), "tool_calls": []}
                else:
                    reply = await self.provider.chat_with_tools(messages, schemas)
            except ToolCallingUnsupported:
                no_tools = True
                try:
                    reply = {"content": await self._json_chat(task, messages), "tool_calls": []}
                except Exception as e:  # noqa: BLE001
                    return self._degraded(task, ctx, steps, e)
            except Exception as e:  # noqa: BLE001
                return self._degraded(task, ctx, steps, e)

            calls = reply.get("tool_calls") or []
            if not calls:
                text = (reply.get("content") or "").strip()
                data = self._final_from_text(task, text, ctx)
                if not data:
                    data = self._fallback(task, ctx)
                return AgentResult(
                    text=text,
                    data=data,
                    steps=steps,
                    source="degraded" if no_tools else "llm",
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
            return self._degraded(task, ctx, steps, e)
        data = self._final_from_text(task, text, ctx) or self._fallback(task, ctx)
        return AgentResult(text=text, data=data, steps=steps, source="llm", stopped="max_steps")

    def _final_from_text(self, task: Task, text: str, ctx) -> dict:
        if not text:
            return {}
        if task.parse_final is not None:
            try:
                out = task.parse_final(self, text, ctx)
                if isinstance(out, dict) and out:
                    return out
            except Exception:  # noqa: BLE001
                pass
        return self._validate(task, parse_json(text), ctx)

    async def _json_chat(self, task: Task, messages) -> str:
        msgs = [dict(m) for m in messages if m.get("role") in ("system", "user")]
        if task.json_hint:
            msgs.append({"role": "user", "content": task.json_hint})
        return await self.provider.chat(msgs, json_mode=True)

    def _degraded(self, task: Task, ctx, steps, err) -> AgentResult:
        return AgentResult(
            data=self._fallback(task, ctx),
            steps=steps,
            source="scripted",
            stopped="error",
            error=str(err),
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
