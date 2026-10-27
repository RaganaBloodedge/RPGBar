"""主机端小模型的固定任务：`persona`（人物小传）/ `react_weights`（反应权重）/ `summarize`（滚动摘要）。

它们与玩家端小助手（`action_advice`）**共用一个引擎**（`ToolAgent`）——「主机端与玩家端的
小助手本质是同一个工具代理」，只是挂的任务不同。这些任务都是**固定路径**：单轮 JSON，
不挂工具、不需要工具循环。
"""
from dataclasses import dataclass, field

from ..character_card import normalize_card as _normalize_card
from ..character_card import script_digest as _script_digest
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
    validate=_react_validate,
)


# ---------------------------------------------------------------- summarize

@dataclass
class SummaryCtx:
    """滚动摘要任务的输入：已有概要 + 这一批新发生的剧情条目。"""

    previous: str = ""
    items: list = field(default_factory=list)
    scene: str = ""


SUMMARIZE_SYSTEM = (
    "你是跑团日志的整理者。\n"
    "【你的职责】把「新发生的剧情」并入「已有的剧情概要」，产出一份**更新的滚动摘要**，"
    "让主持人不必回读全部原文也不会忘记前面的事。\n"
    "【要求】\n"
    "- 保留关键角色（尤其是 NPC 的名字与身份）、关键决定、已获得的线索与物品、尚未解决的悬念。\n"
    "- 丢掉过程性废话，不要逐句复述；概要控制在 300 字以内。\n"
    "- 已经有概要时是在它基础上**续写整合**，不要丢掉旧概要里的重要信息。\n"
    "- 只输出 JSON，不要任何解释文字。"
)

SUMMARIZE_JSON_HINT = (
    "只输出如下 JSON：\n"
    '{"summary":"整合后的剧情概要（≤300 字）",'
    '"key_characters":["出现的关键角色"],"open_threads":["尚未了结的悬念"]}'
)


def _summarize_user(agent, ctx) -> str:
    lines = [_world(agent)]
    lines.append(f"【已有概要】{ctx.previous or '（暂无）'}")
    if ctx.scene:
        lines.append(f"【当前场景】{ctx.scene}")
    lines.append("【新发生的剧情】")
    lines.extend(f"- {it}" for it in ctx.items)
    lines.append("请把这些新剧情并入已有概要，输出更新后的摘要。")
    return "\n".join(l for l in lines if l)


def _summarize_validate(agent, data, ctx) -> dict:
    s = str(data.get("summary") or "").strip()
    if not s:
        return {}
    out = {"summary": s[:1200]}
    kc = data.get("key_characters")
    if isinstance(kc, list):
        out["key_characters"] = [str(x).strip() for x in kc if str(x).strip()][:12]
    ot = data.get("open_threads")
    if isinstance(ot, list):
        out["open_threads"] = [str(x).strip() for x in ot if str(x).strip()][:12]
    return out


SUMMARIZE_TASK = Task(
    name="summarize",
    label="剧情滚动摘要",
    system=SUMMARIZE_SYSTEM,
    secondary=_world,
    user=_summarize_user,
    json_hint=SUMMARIZE_JSON_HINT,
    validate=_summarize_validate,
)


# ---------------------------------------------------------------- character_card

CHARACTER_CARD_SYSTEM = (
    "你是一场中文文字跑团（TRPG）的「角色卡设计者」。\n"
    "【你的职责】读一份剧本摘要，判断玩这个本需不需要玩家角色卡；如果需要，"
    "设计出这个本**真正用得上**的属性维度，并给出可自由分配的点数。\n"
    "【判断标准】\n"
    "- 本子有检定（战斗、学识、魅力……）、有需要不同人分工的挑战 ⇒ 需要角色卡。\n"
    "- 规则驱动的沙盒没有预写场景检定时，必须结合规则库的角色创建字段、技能和玩法说明判断，"
    "不能因为当前场景列表为空就直接判定不需要。\n"
    "- 纯叙事 / 纯解谜、成败与人物能力无关的本 ⇒ **不需要**：required 填 false，attributes 留空。\n"
    "- 拿不准时倾向「不需要」——多一张没人看的卡，比少一张更烦人。\n"
    "【设计规则】\n"
    "- 属性 2–6 项，名字要短（≤4 字），贴合这个剧本的世界观与题材，**不要照抄 D&D 六维**。\n"
    "- 如果剧本的检定技能本身就是能力项（力量 / 学识 / 侦查 / 聆听……），属性就**沿用这些名字** ——\n"
    "  玩家一眼能对上，也不会出现「这个技能到底算哪项属性」的歧义。\n"
    "- 每项给 min / base / max：base 是人人白送的基础值（一般 10），玩家用点数在 base 之上加到 max。\n"
    "- total_points 是人人相同的可分配点数（3–12 之间；属性越多给得越多）。\n"
    "- skill_map 写成**列表**，剧本里出现的**每一个**检定技能各一条："
    '{"skill":"战斗","attr":"力量"}。不许漏，也不许凭空造出摘要里没有的技能。\n'
    "- 只输出 JSON，不要任何解释文字。"
)

CHARACTER_CARD_JSON_HINT = (
    "严格按下面的骨架输出（尖括号是占位，不要照抄成一行；attribute 要 2–6 项，"
    "skill_map 要为摘要里的每个技能各写一条）：\n"
    "{\n"
    '  "required": true,\n'
    '  "reason": "<一句话说明为什么需要/不需要>",\n'
    '  "total_points": <3-12 的整数>,\n'
    '  "attributes": [\n'
    '    {"key": "<属性名>", "desc": "<一两句说明这个属性能干什么>", '
    '"min": <下限>, "base": <基础值>, "max": <上限>},\n'
    '    {"key": "<属性名>", ...}\n'
    "  ],\n"
    '  "skill_map": [\n'
    '    {"skill": "<摘要里的某个检定技能>", "attr": "<上面某个属性名>"},\n'
    '    {"skill": "<另一个检定技能>", "attr": "<...>"}\n'
    "  ]\n"
    "}\n"
    "不需要角色卡时：{\"required\": false, \"reason\": \"<原因>\", "
    "\"attributes\": [], \"skill_map\": []}"
)


def _card_user(agent, ctx) -> str:
    store = agent.store
    prompt = _script_digest(store)
    if bool((getattr(store, "data", None) or {}).get("sandbox")):
        prompt += (
            "\n\n【玩法模式】这是规则驱动的动态沙盒。请根据规则库的角色创建字段、"
            "技能和本局玩法判断是否需要角色卡；不要只看当前是否有预设场景。"
        )
        # 这些字段只是证据，最终是否需要角色卡仍由 Agent 判断。
        try:
            from pathlib import Path
            from ..kb_tools import kb_for_script
            from compiler.retrieve import KB

            rel = Path(str(getattr(store, "source", ""))).name
            kb_path = kb_for_script(rel)
            if kb_path:
                kb = KB(str(kb_path), with_vec=False)
                rows = kb.con.execute(
                    "SELECT field_key,label,kind FROM pc_card_schema ORDER BY ord LIMIT 12"
                ).fetchall()
                fields = [f"{r['field_key']}（{r['label']}，{r['kind']}）" for r in rows]
                if fields:
                    prompt += "\n【规则书角色卡字段证据】" + "、".join(fields)
        except Exception:  # noqa: BLE001
            pass
    return prompt + "\n\n请判断这个剧本需不需要角色卡，并按上面的规则输出 JSON。"


def _card_validate(agent, data, ctx) -> dict:
    # 真正的护栏在 character_card.normalize_card 里（区间、点数、skill_map 全部洗一遍）。
    # 它返回 {} = 洗不出来 → 这一轮算没产出，不拿假卡顶替。
    return _normalize_card(data)


CHARACTER_CARD_TASK = Task(
    name="character_card",
    label="分析剧本角色卡",
    system=CHARACTER_CARD_SYSTEM,
    user=_card_user,
    json_hint=CHARACTER_CARD_JSON_HINT,
    validate=_card_validate,
)


class AssistantAgent(ToolAgent):
    """主机端小模型助手：挂 `persona` / `react_weights` / `summarize` / `character_card`。"""

    name = "assistant"
    default_task = "persona"

    def __init__(self, store, provider=None):
        super().__init__(store, provider)
        self.register(PERSONA_TASK)
        self.register(REACT_TASK)
        self.register(SUMMARIZE_TASK)
        self.register(CHARACTER_CARD_TASK)


async def analyze_character_card(store, provider):
    """跑一次「分析剧本 → 角色卡」。

    单独开一个一次性的 `ToolAgent`：这张卡是**剧本级**的（一个本一张，跟着缓存走），
    不属于任何一局，所以不借用房间里的助手实例（免得顺手改了它的任务状态）。
    """
    agent = ToolAgent(store, provider)
    agent.register(CHARACTER_CARD_TASK)
    return await agent.run_task("character_card", None)
