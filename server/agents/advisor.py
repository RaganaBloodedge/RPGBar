"""玩家私有小助手（小模型）——ToolAgent 上的 `action_advice` 任务。

它是「小模型工具代理」的第一条任务模板（见 `tool_agent.py`）。职责边界很清楚——**只读**：
工具集里没有任何能改游戏状态的东西，所以「小助手不会替玩家改剧情」不是靠提示词祈求，
而是靠它拿不到写工具。

- 只读工具：lookup_script（+ finish 收尾）
- **没有 read_scene / list_valid_actions**：场景描述、可走的方向、可尝试的检定全都写在
  每轮的 user 消息里了。小助手模型更小（1.7B），多一个"去查一下"的工具就多一次生成 ——
  而它要回答的问题（给 3 条建议）本来不需要再多一次往返。

这条通道是给「玩家自己机器上的 3B/4B 模型」准备的（也可以是主机端小模型）：
把小助手指向 `http://127.0.0.1:8080/v1` 之类的本地地址即可，算力花在玩家自己身上。
"""
from dataclasses import dataclass

from .base import AgentResult, Tool, _obj, int_prop, str_prop
from .tool_agent import Task, ToolAgent, parse_json


@dataclass
class AdvisorContext:
    state: object
    player_name: str = "冒险者"


ADVISOR_JSON_HINT = (
    "请只输出一个 JSON 对象，不要任何解释文字：\n"
    '{"options":["建议1","建议2","建议3"]}\n'
    "每条建议不超过 15 字，必须是玩家当下可以做的具体行动。"
)

ADVISOR_SYSTEM = (
    "你是玩家在文字跑团里的私人小助手，不是主持人（DM）。\n"
    "【你的职责】\n"
    "1. 看着当前场景，给出 3 条玩家**当下就能做**的具体行动建议。\n"
    "2. 建议要短、要以动词开头（例如「检查石门」「合力抬开石闩」）。\n"
    "【当前场景已经全给你了】\n"
    "用户消息里有场景描述、**可走的方向**、**可尝试的检定** —— 这些就是全部合法选项，"
    "直接据此给建议，不用调工具确认。\n"
    "- 只有想知道剧本别处的设定时，才用 lookup_script 检索。\n"
    "- 最后用 finish(options) 给出建议列表。\n"
    "【硬性边界】\n"
    "- 你只能读，不能改剧情；不替玩家做决定，只给选项。\n"
    "- 每条建议不超过 15 字，且必须是当前场景真实可做的行动。\n"
)


def _valid_actions(scene) -> tuple:
    """当前场景的合法选项 → (出口, 检定)。两边都按「给玩家看的名字」排。"""
    exits = [(e.get("label") or e.get("to"), e.get("to")) for e in scene.get("exits", [])]
    checks = []
    for c in scene.get("checks", []):
        kw = c.get("keywords") or []
        checks.append((kw[0] if kw else c["id"], c["id"]))
    return exits, checks


def _advise_user(agent, ctx) -> str:
    state = ctx.state
    cur = state.current()
    exits, checks = _valid_actions(cur)
    moves = "、".join(label for label, _ in exits) or "（这里没有出口）"
    tries = "、".join(label for label, _ in checks) or "（这里没有明确的检定）"
    return (
        f"当前场景：{cur.get('title', '')}（{cur.get('location', '')}）\n"
        f"地点描述：{cur.get('public_text', '')}\n"
        f"可走的方向：{moves}\n"
        f"可尝试的检定：{tries}\n"
        f"队伍已获线索：{'；'.join(agent.store.flag_label(f) for f in sorted(state.flags)) or '无'}\n"
        f"玩家：{ctx.player_name}\n"
        "请给出 3 条行动建议。"
    )


def _advise_validate(agent, data, ctx) -> dict:
    opts = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()]
    return {"options": opts[:4]}


ADVISE_TASK = Task(
    name="action_advice",
    label="行动建议",
    system=ADVISOR_SYSTEM,
    secondary=lambda agent, ctx: agent.store.script_prompt_public,
    user=_advise_user,
    tools=lambda agent, ctx: agent._advisor_tools(ctx),
    json_hint=ADVISOR_JSON_HINT,
    validate=_advise_validate,
    parse_final=lambda agent, text, ctx: _advise_validate(agent, parse_json(text), ctx),
    max_steps=4,
)


class AdvisorAgent(ToolAgent):
    name = "advisor"
    default_task = "action_advice"

    def __init__(self, store, provider=None):
        super().__init__(store, provider)
        self.register(ADVISE_TASK)

    # ---- 只读工具（结构上拿不到写工具；也没有"去查一下"这两个冗余项）----
    def _advisor_tools(self, ctx) -> list:
        return [
            Tool(
                "lookup_script",
                "在剧本里检索与关键词最相关的片段（想了解别处的设定时才用）。只读。",
                _obj({"query": str_prop("检索关键词"), "k": int_prop("返回条数", 3)}),
                self._t_lookup_script,
            ),
            Tool(
                "finish",
                "给出 2-4 条行动建议并结束。",
                _obj(
                    {
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "建议列表，每条不超过 15 字",
                        }
                    },
                    ["options"],
                ),
                self._t_finish,
            ),
        ]

    # 兼容旧调用：冒烟测试直接调 build_tools(ctx)
    def build_tools(self, ctx) -> list:
        return self._advisor_tools(ctx)

    def _t_lookup_script(self, ctx, query="", k=3):
        k = max(1, min(int(k or 3), 6))
        ids = self.store.retrieve(ctx.state.current_scene, query or "", k=k)
        return {
            "scenes": [
                {"scene_id": sid, "title": self.store.scenes[sid]["title"],
                 "excerpt": self.store.scenes[sid]["public_text"][:160]}
                for sid in ids
            ]
        }

    def _t_finish(self, ctx, options=None):
        opts = [str(o).strip() for o in (options or []) if str(o).strip()]
        return {"options": opts[:4]}

    async def suggest(self, ctx) -> AgentResult:
        """对外入口：返回 AgentResult，建议列表在 data['options']。

        没有建议就是**没有建议**（2026-10-13 用户拍板取消脚本化兜底）。以前模型
        没按协议给列表时会退回 `fallback_options()` —— 从剧本里抄出口与检定关键词，
        看着像助手给了建议，其实是把当前场景的信息换个地方又念了一遍。现在直接
        标 failed，界面会说明这一轮助手没有产出。
        """
        res = await self.run_task("action_advice", ctx)
        opts = [str(o).strip() for o in (res.data.get("options") or []) if str(o).strip()]
        if not opts:
            res.source = "failed"
            res.error = res.error or "模型没有给出建议列表"
            res.data = {"options": []}
            return res
        res.data = {"options": opts[:4]}
        return res
