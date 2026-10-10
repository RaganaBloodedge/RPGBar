"""主机端小模型的两条固定任务：`persona`（生成人物小传）与 `react_weights`（算反应权重）。

它们与玩家端小助手（`action_advice`）**共用一个引擎**（`ToolAgent`）——「主机端与玩家端的
小助手本质是同一个工具代理」，只是挂的任务不同。两条任务都是**固定路径**：单轮 JSON，
不挂工具、不需要工具循环。
"""
from .tool_agent import Task, ToolAgent


def _world(agent, ctx=None) -> str:
    """剧本世界观（不含 DM 内幕）。人物设定要贴合世界，但不必知道主持人的底牌。"""
    st = getattr(agent, "store", None)
    if st is None:
        return ""
    parts = [f"【剧本】《{getattr(st, 'title', '')}》"]
    premise = getattr(st, "premise", "") or ""
    system = getattr(st, "system", "") or ""
    if premise:
        parts.append(f"【背景】{premise}")
    if system:
        parts.append(f"【基调】{system}")
    return "\n".join(parts)


# ---------------------------------------------------------------- persona

PERSONA_SYSTEM = (
    "你是一场中文文字跑团（TRPG）里的「人物设定师」。\n"
    "【你的职责】为一个次要 NPC 生成一份简短、可长期复用的人物小传，让他在后续互动里有一致的性格。\n"
    "【要求】\n"
    "- 贴合剧本的世界观与基调；这个人要像那个世界里真实存在的人，不要脸谱化。\n"
    "- 给出 6 项 0~1 的特质：胆量、攻击性、守序、记仇、贪财、好说话（0=极低，1=极高）。\n"
    "- 口吻要具体（说话方式、口头习惯）；目标、恐惧各 1~2 条。\n"
    "- 若玩家很可能给他起外号，可给出 0~2 个「别称」。\n"
    "- 只输出 JSON，不要任何解释文字。"
)

PERSONA_JSON_HINT = (
    "只输出如下 JSON：\n"
    '{"name":"姓名","role":"身份","summary":"一两句小传",'
    '"traits":{"胆量":0.3,"攻击性":0.2,"守序":0.5,"记仇":0.2,"贪财":0.5,"好说话":0.6},'
    '"voice":"口吻","goals":["..."],"fears":["..."],"knows":"他掌握的信息","aliases":["可能的别称"]}'
)


def _persona_user(agent, ctx) -> str:
    lines = [_world(agent)]
    if ctx.scene_text:
        lines.append(f"【当前场景】{ctx.scene_text}")
    who = ctx.name + (f"（{ctx.role}）" if ctx.role else "")
    lines.append(f"【要设定的 NPC】{who}")
    lines.append("请为这个 NPC 生成人物小传。")
    return "\n".join(l for l in lines if l)


def _persona_validate(agent, data, ctx) -> dict:
    if not str(data.get("name") or "").strip():
        return {}
    return data


PERSONA_TASK = Task(
    name="persona",
    label="生成人物小传",
    system=PERSONA_SYSTEM,
    secondary=_world,
    user=_persona_user,
    json_hint=PERSONA_JSON_HINT,
    fallback=lambda agent, ctx: {},
    validate=_persona_validate,
)


# ---------------------------------------------------------------- react_weights

REACT_SYSTEM = (
    "你是 NPC 行为模拟器。\n"
    "【你的职责】根据某个 NPC 的人物卡与当下情境，给出他/她**可能做出的几种反应**，以及各自的权重。\n"
    "【硬性规则】\n"
    "- 权重是 0~1 的相对大小，不必和为 1（系统会归一化）；请让权重真实反映此人性格——"
    "例如一个老实的普通人，报警的概率就应该很低。\n"
    "- 给 1~5 个**互斥**的反应选项，每个用 2~8 个字概括（如「忍气吞声」「花小钱了事」「呼救报官」「反击」「逃跑」）。\n"
    "- 你**不得**输出任何骰子点数或随机结果——随机由系统完成。\n"
    "- 只输出 JSON，不要任何解释文字。"
)

REACT_JSON_HINT = (
    "只输出如下 JSON：\n"
    '{"options":[{"label":"忍气吞声","weight":0.4},{"label":"花小钱了事","weight":0.35},'
    '{"label":"呼救报官","weight":0.1},{"label":"反击","weight":0.1},{"label":"逃跑","weight":0.05}]}'
)


def _react_user(agent, ctx) -> str:
    card = ctx.card
    npc = ctx.npc
    lines = [f"【人物卡】{npc.card_brief(card)}"]
    lines.append(f"【情境】{ctx.situation}")
    lines.append("请给出该 NPC 的反应选项与权重。")
    return "\n".join(l for l in lines if l)


def _react_validate(agent, data, ctx) -> dict:
    raw = data.get("options") or []
    out = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or "").strip()
        if not label:
            continue
        try:
            w = max(0.0, float(o.get("weight", 0)))
        except (TypeError, ValueError):
            w = 0.0
        out.append({"label": label, "weight": w})
    out = out[:5]
    return {"options": out} if out else {}


REACT_TASK = Task(
    name="react_weights",
    label="NPC 反应权重",
    system=REACT_SYSTEM,
    secondary=_world,
    user=_react_user,
    json_hint=REACT_JSON_HINT,
    fallback=lambda agent, ctx: {},
    validate=_react_validate,
)


class AssistantAgent(ToolAgent):
    """主机端小模型助手：目前挂 `persona` 与 `react_weights`（后续再加 `summarize`）。"""

    name = "assistant"
    default_task = "persona"

    def __init__(self, store, provider=None):
        super().__init__(store, provider)
        self.register(PERSONA_TASK)
        self.register(REACT_TASK)
