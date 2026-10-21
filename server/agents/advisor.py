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
    chronicle: object = None  # Chronicle（可空）；给了就能把「最近发生」写进 user 消息


ADVISOR_JSON_HINT = (
    "请只输出一个 JSON 对象，不要任何解释文字：\n"
    '{"options":["建议1","建议2","建议3"]}\n'
    "每条建议是一句 15-30 字的完整句子，说明为什么现在做这件事。"
)

ADVISOR_SYSTEM = (
    "你是玩家在文字跑团里的私人小助手，不是主持人（DM）。\n"
    "【你的职责】\n"
    "1. 根据**当前局势**给出 3 条玩家当下就能做的具体行动建议。\n"
    "2. 「贴合局势」的意思：结合最近发生的剧情、刚获得的线索、场景里显眼的东西——\n"
    "   例如刚发现地砖拖痕就建议「顺着拖痕查看」，刚拿到护身符就建议「向卡特出示护身符」。\n"
    "   每条建议都应该是这一刻做才有意义的事，不是任何时候都成立的通用动作。\n"
    "3. 每条是一句 15-30 字的完整建议，动词开头，把「为什么是现在」说进去。\n"
    "4. **每条建议里必须至少带上一个触发说法的原词**——玩家照着你的建议说，\n"
    "   才会真的走进出口 / 触发检定。改写句子可以，把触发原词嵌进去。\n"
    "【改写示范（换了一个完全不同的场景，只学写法，别抄内容）】\n"
    "清单里写着「通往 gate_house（触发说法：进堡/敲堡门/绕后墙）」，\n"
    "此刻你刚发现吊桥的木板有新脚印——\n"
    "  ✗ 照抄：「进堡」（跟清单一模一样的短语，不许出现）\n"
    "  ✗ 丢了触发词：「沿着新脚印查吊桥」（说了也不会触发进堡）\n"
    "  ✓ 改写：「新脚印指向堡内——敲堡门进堡，留意守卫的反应」\n"
    "检定「贿赂守卫（口才检定）」，此刻守卫刚打了个哈欠——\n"
    "  ✓ 改写：「守卫看着困，趁现在上前贿赂守卫换条通路」\n"
    "【合法范围】\n"
    "用户消息里「系统确认的合法选项」（出口与检定）是这场戏里真正有机制的动作。\n"
    "建议要么取自它们，要么是它们的**换说法变体**——不得编造不存在的出口、检定或物品。\n"
    "给检定性建议时，措辞要贴近它列出的触发说法（玩家照着说才会真的触发骰子）。\n"
    "【当前场景已经全给你了】\n"
    "用户消息里有场景描述、最近发生的事、**可走的方向**、**可尝试的检定** —— 不用调工具确认。\n"
    "- 只有想知道剧本别处的设定时，才用 lookup_script 检索。\n"
    "- 最后用 finish(options) 给出建议列表。\n"
    "【硬性边界】\n"
    "- 你只能读，不能改剧情；不替玩家做决定，只给选项。\n"
    "- **禁止照抄选项清单原文**——同一个出口，换一个针对当前局势的说法。\n"
)


def _valid_actions(scene) -> tuple:
    """当前场景的合法选项 → (出口显示, 检定显示)。

    刻意**不写出口的 label 原文**——小模型会把清单当答案抄（实测 1.7B：
    给「钻进石门」它就回「钻进石门」）。这里只给「通往哪个场景 + 触发说法」，
    让它自己组句；触发说法保留，玩家照着说才会真的触发骰子。
    """
    exits = []
    for e in scene.get("exits", []):
        kw = "/".join((e.get("keywords") or [])[:5])
        to = e.get("to") or "?"
        exits.append(f"通往 {to} 的路（触发说法：{kw}）" if kw else f"通往 {to} 的路")
    checks = []
    for c in scene.get("checks", []):
        kw = c.get("keywords") or []
        body = "/".join(kw[:4]) if kw else c["id"]
        checks.append(f"{body}（{c['skill']}检定）")
    return exits, checks


def _advise_user(agent, ctx) -> str:
    state = ctx.state
    cur = state.current()
    exits, checks = _valid_actions(cur)
    moves = "；".join(exits) or "（这里没有出口）"
    tries = "；".join(checks) or "（这里没有明确的检定）"
    ch = getattr(ctx, "chronicle", None)
    hist = ch.digest(4) if ch is not None else ""
    hist_block = f"最近发生：\n{hist}\n" if hist else ""
    flags = sorted(state.flags)
    flag_block = "；".join(agent.store.flag_label(f) for f in flags) or "无"
    return (
        f"当前场景：{cur.get('title', '')}（{cur.get('location', '')}）\n"
        f"地点描述：{cur.get('public_text', '')}\n"
        f"{hist_block}"
        f"队伍已获得线索：{flag_block}\n"
        f"系统确认的合法选项（建议必须是其中之一或其换说法变体，不得编造）：\n"
        f"- 可走的方向：{moves}\n"
        f"- 可尝试的检定：{tries}\n"
        f"玩家：{ctx.player_name}\n"
        "请给 3 条贴合当前局势的建议：每条写成 15-30 字的完整句子，"
        "结合最近发生的事与已获得的线索说明「为什么是现在做这件事」，"
        "绝对不要输出与上面清单一模一样的短语。"
    )


def _advise_validate(agent, data, ctx) -> dict:
    opts = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()]
    # 确定性过滤（不靠提示词求小模型）：照抄出口 label / 触发词原文、
    # 或不足 8 字的「清单式短语」一律丢弃——宁可少给，不给复读。
    banned = set()
    scene = {}
    try:
        scene = ctx.state.current() or {}
    except Exception:
        pass
    for e in scene.get("exits", []):
        if e.get("label"):
            banned.add(str(e["label"]).strip())
        for kw in e.get("keywords") or []:
            banned.add(str(kw).strip())
    for c in scene.get("checks", []):
        for kw in c.get("keywords") or []:
            banned.add(str(kw).strip())
    kept = [o for o in opts if o not in banned and len(o) >= 8]
    # 机制可达性优先，但不过分牺牲数量（1.7B 经常整批不带触发词）：
    # 候选 ≥3 条时才优先挑含触发词的；否则保留所有干净成句的建议。
    if len(kept) >= 3:
        trigger_words = [w for w in banned if len(w) >= 2]
        hits = [o for o in kept if any(w in o for w in trigger_words)]
        if hits:
            kept = hits
    # 退化次序：命中触发词的 > 不照抄但成句的（kept）> 原始列表（聊胜于无）
    return {"options": (kept or opts)[:4]}


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
                            "description": "建议列表，每条一句 15-30 字的完整句子",
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
        # 收口再过一次校验：finish 工具路径与 parse_final 路径都要吃同样的
        # 确定性过滤（丢照抄、丢短句、优先留带触发词的）——2026-10-21 实测
        # finish 工具的返回不会走 Task.validate，示例原文会从这里漏出去。
        try:
            opts = _advise_validate(self, {"options": opts}, ctx).get("options") or []
        except Exception:
            pass
        if not opts:
            res.source = "failed"
            res.error = res.error or "模型没有给出建议列表"
            res.data = {"options": []}
            return res
        res.data = {"options": opts[:4]}
        return res
