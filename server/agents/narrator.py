"""DM（主机端，大模型）：推进剧情的 NarratorAgent。

护栏从「事后校验 LLM 的 JSON」改成了「工具本身只在合法范围内生效」——
这是标准 Agent 写法：模型能做什么，由它拿到的工具决定。

- 只读工具：read_scene / list_exits / list_checks / lookup_script
- 写工具（会改游戏状态）：roll_check / move_to / set_flag
  - move_to 只接受「当前场景列出的、且条件已满足的出口」，否则返回错误让模型重试
  - set_flag 只接受剧本声明过的 flag
  - roll_check 由服务端投骰，模型拿不到骰子，也就编不出结果
- finish：模型显式收尾

没有模型（用户留空 API）时，整个 Agent 退化成 dm.py 的确定性流水线，
玩法不变——**有模型走工具循环，没模型走剧本化编排，两者共用同一套状态机**。
"""
from dataclasses import dataclass, field

from .. import dice
from ..dm import DM
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


class NarratorAgent(Agent):
    name = "narrator"
    max_steps = 6
    json_hint = NARRATOR_JSON_HINT

    def __init__(self, store, provider=None, npc=None):
        super().__init__(provider)
        self.store = store
        self.dm = DM(store, provider)
        self.npc = npc  # NpcService（可空）；DM 只经它读人物卡，拿不到 NPC 的记忆流水账

    # ---- 提示词 ----
    # 第一层：System prompt —— 只写「我是谁、我能做什么、我的边界」，与具体剧本无关。
    def system_prompt(self, ctx) -> str:
        return (
            "你是一场中文文字跑团（TRPG）的主持人（DM）。\n"
            "【你的职责】\n"
            "1. 依据给定的剧本，为玩家行动给出 2-4 句简短、有画面感的旁白，推动剧情前进。\n"
            "2. 判定玩家意图：命中检定就投骰，命中出口就推进场景，其余自由发挥。\n"
            "3. 扮演剧本中的 NPC，但不替玩家做决定。\n"
            "【工作方式】\n"
            "- 先用只读工具了解现状（read_scene / list_exits / list_checks / lookup_script），再决定动作。\n"
            "- 要推进场景就调 move_to；要投骰就调 roll_check；剧情明确产生新线索才调 set_flag。\n"
            "- 需要某个 NPC 的设定时用 npc_card；出场了剧本里没有的新 NPC 时用 npc_introduce 建档。\n"
            "- 收尾时调 finish(narration) 给出最终旁白；若无需推进任何东西，直接给旁白也可以。\n"
            "【硬性边界】\n"
            "- 严格遵循给定的剧本，不得编造剧本之外的走向、NPC、物品或地点。\n"
            "- 骰子只能由 roll_check 产生，你不得编造任何骰子结果，也不要在旁白里报出数值。\n"
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
            f"{p.name}({p.character.get('cls', '')})" for p in state.players.values()
        )
        npc_block = f"\n【在场 NPC（人物卡）】\n{ctx.npc_brief}" if getattr(ctx, "npc_brief", "") else ""
        return (
            f"{self.store.build_context(state.current_scene, user_input)}\n\n"
            f"当前场景：{state.current_scene} 地点：{cur.get('location', '')}\n"
            f"已获得 flag：{sorted(state.flags) or '无'}\n"
            f"在场玩家：{players}"
            f"{npc_block}\n"
            f"玩家行动：{user_input}"
        )

    # ---- 工具 ----
    def build_tools(self, ctx) -> list:
        tools = [
            Tool(
                "read_scene",
                "读取某个场景的完整内容（含 DM 内幕）。省略 scene_id 则读当前场景。只读。",
                _obj({"scene_id": str_prop("场景 id，省略则当前场景")}),
                self._t_read_scene,
            ),
            Tool(
                "list_exits",
                "列出当前场景的全部出口，以及每个出口的条件是否已满足。只读。",
                _obj(),
                self._t_list_exits,
            ),
            Tool(
                "list_checks",
                "列出当前场景可触发的检定（id / 技能 / 难度）。只读。",
                _obj(),
                self._t_list_checks,
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
        tools.extend(
            [
                Tool(
                    "roll_check",
                    "让服务端投掷某个检定并应用结果（可能获得线索）。这是唯一产生骰子结果的途径。",
                    _obj({"check_id": str_prop("检定 id，见 list_checks")}, ["check_id"]),
                    self._t_roll_check,
                    read_only=False,
                ),
                Tool(
                    "move_to",
                    "把队伍推进到某个出口指向的场景。仅当该出口存在且条件已满足时生效。",
                    _obj({"scene_id": str_prop("目标场景 id，见 list_exits")}, ["scene_id"]),
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

    def _t_list_exits(self, ctx):
        cur = ctx.state.current()
        return {
            "exits": [
                {"scene_id": e["to"], "label": e["label"], "unlocked": DM._exit_ok(e, ctx.state)}
                for e in cur.get("exits", [])
            ]
        }

    def _t_list_checks(self, ctx):
        return {
            "checks": [
                {"check_id": c["id"], "skill": c["skill"], "dc": c.get("dc", 10)}
                for c in ctx.state.current().get("checks", [])
            ]
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
        r = dice.roll_check(check["skill"], check.get("dc", 10))
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
                "success": ok,
                "flag": check.get("success_flag") if ok else None,
            }
        )
        ctx.narration_parts.append(outcome)
        return {
            "rolled": True,
            "success": ok,
            "flag_gained": check.get("success_flag") if ok else None,
            "note": "系统已投骰并广播结果，你只需补一句简短衔接。",
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
            "note": "系统已广播新场景原文，你只需补一句简短衔接（可以留空）。",
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

    # ---- 无 provider：交给确定性流水线 ----
    async def run(self, ctx, user_input: str = "") -> AgentResult:
        if self.provider is None:
            ctx.events.extend(await self.dm.handle_action(ctx.state, ctx.player_name, user_input))
            return AgentResult(text="", source="scripted", stopped="no_provider")
        return await super().run(ctx, user_input)

    def fallback_text(self, ctx, user_input: str) -> str:
        return self.dm._scripted_narration(ctx.state.current(), user_input)


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
