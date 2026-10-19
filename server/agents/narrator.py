"""DM（主机端，大模型）：推进剧情的 NarratorAgent。

护栏从「事后校验 LLM 的 JSON」改成了「工具本身只在合法范围内生效」——
这是标准 Agent 写法：模型能做什么，由它拿到的工具决定。

- 只读工具：read_scene / lookup_script（当前场景的原文、出口、检定已经整段注入
  user 消息，所以**没有** list_exits / list_checks —— 那两次调用纯属重复劳动）
- 写工具（会改游戏状态）：roll_check / move_to / set_flag
  - move_to 只接受「当前场景列出的、且条件已满足的出口」，否则返回错误让模型重试
  - set_flag 只接受剧本声明过的 flag
  - roll_check 由服务端投骰，模型拿不到骰子，也就编不出结果
- finish：模型显式收尾

**没有模型就没有产出**（2026-10-13 用户拍板取消脚本化兜底）：不再退化成
`dm.py` 的确定性流水线去拼一段旁白冒充 DM，而是**直接失败** —— 界面会明说
这一轮没有生成。要玩就必须有一个模型（本地引擎或云 API）。
"""
from dataclasses import dataclass, field

from .. import character_card, dice
from ..dm import DM  # 只为 DM._exit_ok（出口门控判定）；不再用它的脚本化流水线
from .base import Agent, AgentResult, Tool, _obj, int_prop, str_prop


@dataclass
class NarratorContext:
    """一次 DM 行动所需的上下文（state 是权威状态，events/parts 是本轮产物）。"""

    state: object
    player_name: str = "冒险者"
    events: list = field(default_factory=list)          # 待广播的事件（dice/system）
    narration_parts: list = field(default_factory=list)  # 系统自己产出的权威旁白
    npc_brief: str = ""  # 本轮涉及 NPC 的「人物卡 + 状态快照 + 反应采样结论」（不含记忆流水账）


NARRATOR_JSON_HINT = {
    "role": "user",
    "content": (
        "该模型无法使用工具。请只输出一个 JSON 对象，不要任何解释文字：\n"
        '{"narration":"2-4 句旁白","move_to":"场景id或null","set_flags":["flag或空数组"]}\n'
        "move_to 只能是当前场景已列出的出口；set_flags 只能使用提示中给出的合法 flag。"
    ),
}

# 「这句话是不是在宣称检定成功了」——用来兜住 4B 最常犯的那个错：
# 骰子判失败，旁白却写「石闩被缓缓抬起」。命中就丢掉模型这一段。
_SUCCESS_WORDS = (
    "成功", "发现", "拿到", "得手", "打开", "推开", "抬起", "破解",
    "找到", "取下", "识破", "看穿", "撬开", "砸开",
)
_NEGATIONS = ("没", "未", "无", "不", "别")


def asserts_success(text: str) -> str:
    """文本里有没有「做到了」的断言；排除「未能发现」「空无一物」这类否定/中性写法。

    命中就返回那个词（用于日志与测试），没命中返回空串。
    只看词前 4 个字里有没有否定词 —— 中文的否定几乎都贴着动词。
    """
    for word in _SUCCESS_WORDS:
        i = text.find(word)
        while i >= 0:
            if not any(n in text[max(0, i - 4):i] for n in _NEGATIONS):
                return word
            i = text.find(word, i + 1)
    return ""


def echoes_text(text: str, source: str, probe: int = 40) -> bool:
    """旁白是不是把已经播报过的原文整段抄了一遍（取前 40 字做探针）。"""
    src = (source or "").strip()
    if len(src) < probe or not text:
        return False
    return src[:probe] in text


class NarratorAgent(Agent):
    name = "narrator"
    max_steps = 6
    json_hint = NARRATOR_JSON_HINT

    def __init__(self, store, provider=None, npc=None, chronicle=None):
        super().__init__(provider)
        self.store = store
        self.npc = npc  # NpcService（可空）；DM 只经它读人物卡，拿不到 NPC 的记忆流水账
        self.chronicle = chronicle  # Chronicle（可空）；DM 只经 recall_history 按需检索，改不了档案

    async def run(self, ctx, user_input: str = "") -> AgentResult:
        """跑一轮，并在返回前**核一遍旁白与权威结果是否打架**。

        两种打架都会把模型那一段丢掉（`drop_reason` 留痕，测试用它断言）：

        1. 本轮有失败的检定，旁白却写成成功 —— 实测 27% 的失败检定点会这样；
        2. 刚换场景，旁白把系统已经播报过的场景原文又抄一遍 —— 实测 24% 的推进轮。

        丢掉不等于「没产出」：权威文案（检定结果 / 新场景原文）本来就是系统自己广播的，
        玩家一个字都不会少。宁可少一句衔接，也不给一句自相矛盾的话。
        """
        before = ctx.state.current_scene
        res = await super().run(ctx, user_input)
        reason = self._guard_narration(ctx, res.text or "", before)
        if reason:
            res.drop_reason = reason
            res.text = ""
        return res

    def _guard_narration(self, ctx, text: str, before_scene: str) -> str:
        if not (text or "").strip():
            return ""
        # 工具返回值里那句「系统已广播新场景原文，你只需补一句衔接」被 4B 原样念出来过
        # （`系统已广播新场景原文。甬道深处传来石门的低鸣…`）—— 玩家不该看见系统指令。
        if "系统已广播" in text or "你只需补一句" in text:
            return "把系统指令念成了旁白"
        if any(e.get("type") == "dice" and not e.get("success") for e in ctx.events):
            word = asserts_success(text)
            if word:
                return f"检定失败却写成成功（{word}）"
        if ctx.state.current_scene != before_scene:
            if echoes_text(text, ctx.state.current().get("public_text", "")):
                return "复述了系统刚播报的场景原文"
        # 热层里存着最近几条旁白原文，模型会直接抄回来 —— 实测越界局里
        # 连续两轮一字不差，其中一轮骰子还是反的。逐字相同的旧旁白一律丢掉：
        # 这一轮本来也没有新信息，少一句好过给一句假话。
        if self.chronicle is not None:
            t = text.strip()
            for e in self.chronicle.entries[-4:]:
                if e.get("kind") == "narration" and (e.get("text") or "").strip() == t:
                    return "与之前某轮的旁白逐字重复"
        return ""

    # ---- 提示词 ----
    # 第一层：System prompt —— 只写「我是谁、我能做什么、我的边界」，与具体剧本无关。
    def system_prompt(self, ctx) -> str:
        return (
            "你是一场中文文字跑团（TRPG）的主持人（DM）。\n"
            "【你的职责】\n"
            "1. 依据给定的剧本，为玩家行动给出 2-4 句简短、有画面感的旁白，推动剧情前进。\n"
            "2. 判定玩家意图：该投骰就投骰，该换场景就换场景，其余自由发挥。\n"
            "3. 扮演剧本中的 NPC，但不替玩家做决定。\n"
            "【当前场景已经全给你了】\n"
            "用户消息的【当前场景】里有：原文、DM 内幕、在场 NPC、"
            "**每个出口及其解锁状态**、**可触发的检定（id / 技能 / DC）**。\n"
            "这就是判断这一轮所需的全部信息 —— **不要再为了「确认一下」去调工具**。\n"
            "- 要换场景：move_to（出口见【当前场景】）。它的返回值会带上新场景的完整信息，不必再读。\n"
            "- 要投骰：roll_check（检定 id 见【当前场景】）。\n"
            "- 只有这几种才需要查：剧本别处的设定用 lookup_script；"
            "某个 NPC 的过往用 npc_card 或 recall_history；出场了剧本里没有的新 NPC 用 npc_introduce。\n"
            "- 剧情明确产生新线索时才 set_flag。\n"
            "【硬性边界】\n"
            "- 严格遵循给定的剧本，不得编造剧本之外的走向、NPC、物品或地点。\n"
            "- 骰子只能由 roll_check 产生，你不得编造任何骰子结果，也不要在旁白里报出数值。\n"
            "- **检定失败就写失败**：说「没做到、一无所获、看不出所以然」，"
            "绝不写门被打开、东西被拿到、线索被发现。\n"
            "- **新场景的原文系统已经播报了**：move_to 之后只补一句衔接，不要复述原文。\n"
            "- **剧本之外的动作不要顺着他演**：玩家要生火、挖墙、抢人东西、骂人、跳过剧情，"
            "就用一句话说明这里做不到（或没人理他），然后把选择权交回给玩家。\n"
            "- **玩家问规则或元问题**（能不能存档、你是不是 AI、这关怎么过），"
            "用一句话直接回答，不要当没听见，也不要借机推进剧情或投骰。\n"
            "- 剧本中标注为「DM 内幕」的内容是你的底牌，不能直接说出口。\n"
            "- move_to / roll_check 调用后系统会自行广播权威结果，你的最终旁白只补一句衔接（可以留空）。\n"
        )

    # 第二层：次级 prompt —— 剧本内容（世界观/语气/切片索引），随剧本切换而变。
    def secondary_prompt(self, ctx) -> str:
        return self.store.script_prompt

    def build_user_message(self, ctx, user_input: str) -> str:
        state = ctx.state
        cur = state.current()
        players = "；".join(
            character_card.player_line(p.name, p.character, getattr(self.store, "card_schema", None))
            for p in state.players.values()
        )
        npc_block = f"\n【在场 NPC（人物卡）】\n{ctx.npc_brief}" if getattr(ctx, "npc_brief", "") else ""
        hist = self.chronicle.digest(6) if self.chronicle is not None else ""
        hist_block = f"\n【剧情档案（热层）】\n{hist}\n（需要更早的细节时用 recall_history 检索）" if hist else ""
        return (
            f"{self.store.build_context(state.current_scene, user_input, flags=state.flags)}\n\n"
            f"当前场景：{state.current_scene} 地点：{cur.get('location', '')}\n"
            f"已获得 flag：{sorted(state.flags) or '无'}\n"
            f"在场玩家：{players}"
            f"{hist_block}"
            f"{npc_block}\n"
            f"玩家行动：{user_input}"
        )

    # ---- 预热 ----
    async def warmup(self, ctx) -> bool:
        """用**真实长度**的 prompt 空转一次前向，把首次 prefill 的成本提前付掉。

        为什么值得：实测（4B Q4 / RTX 5060）DM 的第一次生成，2000 token 的 prompt 要
        26 秒；同样长度第二次只要 7–10 秒。那 26 秒是引擎按序列长度做首次 prefill /
        kernel 准备，不是模型本身慢。玩家进房之后本来就在等待室里等 —— 正好付在这里。

        **不给工具**这句话现在只对"执行"成立：为了让 prefill 的长度与真实一轮**一致**，
        这里仍然把工具 schema 发过去（它占 ~510 token，是 prompt 的大头之一），只是拿到
        的 `tool_calls` 一律丢弃 —— 没有 handler 会跑，状态改不了。
        `provider.needs_warmup` 让云 API / 本地服务直接跳过（首字延迟不在我们这边）。
        """
        if self.provider is None or not getattr(self.provider, "needs_warmup", False):
            return False
        msgs = [
            {"role": "system", "content": self.system_prompt(ctx)},
            {"role": "system", "content": self.secondary_prompt(ctx)},
            {"role": "user", "content": self.build_user_message(ctx, "（预热，请只回复「好」）")},
        ]
        # schema 发过去只为凑长度；返回值丢掉
        await self.provider.chat_with_tools(msgs, [t.schema() for t in self.build_tools(ctx)])
        return True

    # ---- 工具 ----
    # **只有 6 个**，而且刻意不含 list_exits / list_checks：两者要回答的问题
    # （这个场景能往哪走、有什么检定）在【当前场景】里已经写好了，出口甚至带着
    # 解锁状态。曾经实测一轮行动 6 次生成、55 秒，其中 3 次就是模型在「确认」这两件事
    # 和重读当前场景 —— 信息重复提供 + 提示词命令它去查，它当然照办。
    def build_tools(self, ctx) -> list:
        tools = [
            Tool(
                "read_scene",
                "读取**其他**场景的原文与内幕（当前场景已在上下文里，不要为了确认而重读）。只读。",
                _obj({"scene_id": str_prop("场景 id；省略则读当前场景")}),
                self._t_read_scene,
            ),
            Tool(
                "lookup_script",
                "在剧本里检索与关键词最相关的片段（RAG）。只读。",
                _obj({"query": str_prop("检索关键词"), "k": int_prop("返回条数", 3)}),
                self._t_lookup_script,
            ),
        ]
        if self.npc is not None:
            tools.extend(
                [
                    Tool(
                        "npc_card",
                        "查看某个 NPC 的人物卡与当前状态（不含他的历史流水账）。只读。",
                        _obj({"name": str_prop("NPC 名字或别称")}),
                        self._t_npc_card,
                    ),
                    Tool(
                        "npc_introduce",
                        "为剧本里没有的新 NPC 建档（会生成人物小传），让他后续行为保持一致。",
                        _obj(
                            {"name": str_prop("NPC 名字"), "role": str_prop("身份，可选")},
                            ["name"],
                        ),
                        self._t_npc_introduce,
                        read_only=False,
                    ),
                ]
            )
        if self.chronicle is not None:
            tools.append(
                Tool(
                    "recall_history",
                    "检索本局此前发生过的剧情（旁白原文 / 玩家行动 / 骰点结论），用来回忆关键角色与事件。只读。",
                    _obj(
                        {
                            "query": str_prop("检索关键词，如某个 NPC 的名字或一桩事件"),
                            "k": int_prop("返回条数", 3),
                            "actor": str_prop("只看与这个角色相关的记录，可选"),
                        }
                    ),
                    self._t_recall_history,
                )
            )
        tools.extend(
            [
                Tool(
                    "roll_check",
                    "让服务端投掷某个检定并应用结果（可能获得线索）。这是唯一产生骰子结果的途径。",
                    _obj({"check_id": str_prop("检定 id，见【当前场景】的检定列表")}, ["check_id"]),
                    self._t_roll_check,
                    read_only=False,
                ),
                Tool(
                    "move_to",
                    "把队伍推进到某个出口指向的场景。仅当该出口存在且条件已满足时生效。",
                    _obj({"scene_id": str_prop("目标场景 id，见【当前场景】的出口")}, ["scene_id"]),
                    self._t_move_to,
                    read_only=False,
                ),
                Tool(
                    "set_flag",
                    "记录一条剧情线索（只允许剧本声明过的 flag）。",
                    _obj({"flag": str_prop("flag 名")}, ["flag"]),
                    self._t_set_flag,
                    read_only=False,
                ),
                Tool(
                    "finish",
                    "给出最终旁白并结束本轮。",
                    _obj({"narration": str_prop("2-4 句最终旁白")}, ["narration"]),
                    self._t_finish,
                ),
            ]
        )
        return tools

    def _t_npc_card(self, ctx, name=""):
        if self.npc is None:
            return {"error": "本局未启用 NPC 子系统"}
        card = self.npc.registry.resolve((name or "").strip())
        if not card:
            return {"error": f"没有名为「{name}」的 NPC", "known": [c["name"] for c in self.npc.index()]}
        return self.npc.card_public(card)

    async def _t_npc_introduce(self, ctx, name="", role=""):
        if self.npc is None:
            return {"error": "本局未启用 NPC 子系统"}
        card = await self.npc.ensure_card((name or "").strip(), role=(role or "").strip())
        if not card:
            return {"error": "无法为该 NPC 建档", "name": name}
        return {"ok": True, "id": card.id, "name": card.name, "role": card.role, "known": card.summary[:80]}

    def _t_read_scene(self, ctx, scene_id=""):
        sid = (scene_id or "").strip() or ctx.state.current_scene
        s = self.store.get_scene(sid)
        if not s:
            return {"error": f"未知场景：{sid}", "known": sorted(self.store.scenes)}
        return {
            "id": s["id"],
            "title": s["title"],
            "location": s["location"],
            "public_text": s["public_text"],
            "dm_notes": s.get("dm_notes", ""),
            "npcs": [{"name": n["name"], "role": n["role"]} for n in s.get("npcs", [])],
        }

    def _t_recall_history(self, ctx, query="", k=3, actor=""):
        if self.chronicle is None:
            return {"error": "本局未启用剧情档案"}
        hits = self.chronicle.search((query or "").strip(), k=k, actor=(actor or "").strip() or None)
        # 每条截 300 字：这是一次工具结果，会**留在 messages 里**跟着后续每一次生成
        # 重新 prefill（不像热层那样只回 6 条）。原来截 400 字 × 4 条 ≈ 1000 token。
        return {
            "hits": [
                {
                    "turn": h.get("turn"),
                    "kind": h.get("kind"),
                    "scene": h.get("scene"),
                    "text": (h.get("text") or "")[:300],
                }
                for h in hits
            ],
            "note": "以上是本局档案里的原文摘录，请据此叙述，不要编造没有记录的事。",
        }

    def _t_lookup_script(self, ctx, query="", k=3):
        k = max(1, min(int(k or 3), 6))
        ids = self.store.retrieve(ctx.state.current_scene, query or "", k=k)
        return {
            "scenes": [
                {"scene_id": sid, "title": self.store.scenes[sid]["title"],
                 "excerpt": self.store.scenes[sid]["public_text"][:200]}
                for sid in ids
            ]
        }

    def _t_roll_check(self, ctx, check_id=""):
        check_id = (check_id or "").strip()
        scene = ctx.state.current()
        check = next((c for c in scene.get("checks", []) if c["id"] == check_id), None)
        if not check:
            return {
                "error": f"当前场景没有检定 {check_id}",
                "available": [c["id"] for c in scene.get("checks", [])],
            }
        # 属性加值：谁能做成这件事，取决于**行动的人**自己的角色卡。
        # 没填卡 / 这个技能没映射到属性 ⇒ 加值 0，等同于原来的纯 d20 vs DC。
        card = getattr(self.store, "card_schema", None)
        actor = ctx.state.players.get(ctx.player_name)
        mod, attr = character_card.modifier_for(card, (actor.character if actor else {}), check["skill"])
        r = dice.roll_check(check["skill"], check.get("dc", 10), mod=mod)
        ok = r["success"]
        if ok and check.get("success_flag"):
            ctx.state.set_flag(check["success_flag"])
        outcome = check["success"] if ok else check["fail"]
        ctx.events.append(
            {
                "type": "dice",
                "player": ctx.player_name,
                "skill": check["skill"],
                "dc": r["dc"],
                "roll": r["roll"],
                "mod": r["mod"],
                "total": r["total"],
                "attr": attr,
                "success": ok,
                "flag": check.get("success_flag") if ok else None,
            }
        )
        ctx.narration_parts.append(outcome)
        return {
            "rolled": True,
            "success": ok,
            # 把加值的来历告诉 DM：它可以据此说「你的学识让你看出了门道」，
            # 但照旧**不要在旁白里报数字**（提示词里那条硬边界不变）。
            "ability": f"{attr} {mod:+d}" if attr else "",
            "flag_gained": check.get("success_flag") if ok else None,
            # 这一行是给 4B 看的**硬指令**。实测它会把失败当成功写：
            # 力量检定掷出 11 vs DC13（石闩纹丝不动），下一句却是「石闩被缓缓抬起」。
            # 光在 system prompt 里写「遵循结果」不够 —— 工具返回是每轮都在眼前的，
            # 提示词是几十轮前就沉到上下文底部的。
            "note": (
                "成功。旁白可以写他做到了。"
                if ok else
                "**失败**。旁白必须写成「没做到 / 一无所获 / 看不出所以然」，"
                "不得写出只有成功才会出现的结果（门被打开、东西被拿到、线索被发现）。"
            ),
        }

    def _t_move_to(self, ctx, scene_id=""):
        scene_id = (scene_id or "").strip()
        cur = ctx.state.current()
        ex = next((e for e in cur.get("exits", []) if e["to"] == scene_id), None)
        if not ex:
            return {
                "error": f"{scene_id} 不是当前场景的出口",
                "available": [e["to"] for e in cur.get("exits", [])],
            }
        if not DM._exit_ok(ex, ctx.state):
            return {
                "error": f"出口「{ex['label']}」条件未满足",
                "locked": True,
                "required_flag": ex.get("condition"),
            }
        target = self.store.get_scene(scene_id)
        ctx.state.enter(scene_id)
        ctx.state.turn += 1
        ctx.events.append({"type": "system", "text": f"进入：{target['title']}"})
        ctx.narration_parts.append(target.get("public_text", ""))
        return {
            "ok": True,
            "scene_id": scene_id,
            "title": target["title"],
            # 把新场景整段带回去：否则模型下一句一定是 read_scene，白烧一次生成
            # （实测这一跳 3–6 秒，而内容本来就在手里）。
            "scene": self.store.scene_brief(scene_id, flags=ctx.state.flags),
            "note": "系统已广播新场景原文。上面的 scene 就是新场景的全部信息，不用再 read_scene，只需补一句衔接（可以留空）。",
        }

    def _t_set_flag(self, ctx, flag=""):
        flag = (flag or "").strip()
        if flag not in self.store.flag_ids:
            return {"error": f"未知 flag：{flag}", "allowed": sorted(self.store.flag_ids)}
        ctx.state.set_flag(flag)
        return {"ok": True, "flag": flag, "label": self.store.flag_label(flag)}

    def _t_finish(self, ctx, narration=""):
        return {"narration": (narration or "").strip()}

    # ---- 降级：模型不支持工具调用 → 单轮 JSON 协议（与旧行为一致）----
    def parse_final(self, text, ctx):
        obj = _parse_json(text)
        if not obj:
            return None
        move_to = obj.get("move_to")
        if move_to:
            self._t_move_to(ctx, move_to)
        for f in obj.get("set_flags") or []:
            if f in self.store.flag_ids:
                ctx.state.set_flag(f)
        return obj

    # 这里原本有一个 `run()` override：没有 provider 时走 `dm.py` 的确定性流水线，
    # 用剧本原文拼一段旁白冒充 DM。2026-10-13 用户拍板取消脚本化兜底后它被删掉了，
    # 行为完全等于基类：provider is None → source="failed" / stopped="no_provider"。


def _parse_json(raw):
    import json
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
