"""DM 编排：LLM 只提案，服务端校验后应用（护栏）。

处理玩家行动的顺序：
1. 匹配检定（check）→ 服务端投骰、应用结果；
2. 匹配出口（exit）→ 校验条件：满足则推进场景，不满足则返回「被锁」提示；
3. 自由发挥 → 交给 LLM（带 RAG 上下文）叙事，LLM 可提案 move_to / set_flags，
   但必须落在剧本已知的出口与 flag 白名单内；无 LLM 时走脚本化兜底。

匹配采用「命中关键词数 + 最长命中长度」打分，避免「进入/进去」这类泛化词误配。
"""
import json
import re

from . import dice

# flag 白名单：LLM 只能设置剧本里真实存在的 flag
FLAG_WHITELIST = {
    "knows_backdoor_key",
    "found_secret_door",
    "has_key",
    "has_grail",
    "defeated_guardian",
}


class DM:
    def __init__(self, store, provider):
        self.store = store
        self.provider = provider  # None 表示脚本化兜底

    # ---- 匹配 ----
    @staticmethod
    def _score(text, keywords):
        """返回 (命中关键词数, 最长命中关键词长度)。"""
        text = text or ""
        hits = 0
        best_len = 0
        for kw in keywords or []:
            if not kw:
                continue
            if kw in text or (text and text in kw):
                hits += 1
                if len(kw) > best_len:
                    best_len = len(kw)
        return hits, best_len

    def detect_check(self, action, scene):
        best, best_score = None, (0, 0)
        for c in scene.get("checks", []):
            kws = list(c.get("keywords", [])) + [c.get("id", ""), c.get("skill", "")]
            score = self._score(action, kws)
            if score > best_score:
                best_score, best = score, c
        return best if best_score[0] > 0 else None

    def detect_exit(self, action, scene):
        """返回最佳匹配的出口（不判条件，条件由调用方校验），无命中返回 None。"""
        best, best_score = None, (0, 0)
        for e in scene.get("exits", []):
            kws = list(e.get("keywords", [])) + [e.get("to", ""), e.get("label", "")]
            score = self._score(action, kws)
            if score > best_score:
                best_score, best = score, e
        return best if best_score[0] > 0 else None

    @staticmethod
    def _exit_ok(exit_, state):
        cond = exit_.get("condition")
        return (not cond) or (cond in state.flags)

    # ---- 主入口 ----
    async def handle_action(self, state, player_name, action):
        """处理玩家行动，返回需要广播的事件列表，并就地变更状态。"""
        scene = state.current()
        events = []
        state.log(f"{player_name}：{action}")

        check = self.detect_check(action, scene)
        if check:
            return self._run_check(state, player_name, check, events)

        exit_ = self.detect_exit(action, scene)
        if exit_:
            if not self._exit_ok(exit_, state):
                events.append(
                    {
                        "type": "narration",
                        "author": "DM",
                        "text": f"你想「{exit_['label']}」，但眼下还做不到。",
                    }
                )
                return events
            return self._run_exit(state, exit_, events)

        narration = await self._freeform(state, action)
        events.append({"type": "narration", "author": "DM", "text": narration})
        return events

    def _run_check(self, state, player_name, check, events):
        r = dice.roll_check(check["skill"], check.get("dc", 10))
        ok = r["success"]
        if ok and check.get("success_flag"):
            state.set_flag(check["success_flag"])
        events.append(
            {
                "type": "dice",
                "player": player_name,
                "skill": check["skill"],
                "dc": r["dc"],
                "roll": r["roll"],
                "success": ok,
                "flag": check.get("success_flag") if ok else None,
            }
        )
        events.append(
            {
                "type": "narration",
                "author": "DM",
                "text": check["success"] if ok else check["fail"],
            }
        )
        return events

    def _run_exit(self, state, exit_, events):
        target = self.store.get_scene(exit_["to"])
        state.enter(exit_["to"])
        state.turn += 1
        events.append({"type": "system", "text": f"进入：{target['title']}"})
        events.append({"type": "narration", "author": "DM", "text": target.get("public_text", "")})
        return events

    # ---- 自由发挥（LLM / 兜底）----
    async def _freeform(self, state, action):
        if self.provider is None:
            return self._scripted_narration(state.current(), action)
        context = self.store.build_context(state.current_scene, action)
        messages = self._build_messages(state, context, action)
        try:
            raw = await self.provider.chat(messages, json_mode=True)
            obj = _parse_json(raw)
            narration = (obj.get("narration") or "").strip()
            if not narration:
                narration = self._scripted_narration(state.current(), action)
            self._apply_proposal(state, obj)
            return narration
        except Exception:
            return self._scripted_narration(state.current(), action)

    def _build_messages(self, state, context, action):
        cur = state.current()
        legal_exits = "；".join(f"{e['to']}({e['label']})" for e in cur.get("exits", []))
        sys_msg = (
            "你是一场中文文字跑团的 DM。\n"
            + self.store.system
            + "\n【硬性规则】\n"
            "1. 严格遵循剧本，不得编造剧本之外的走向、NPC、物品或地点。\n"
            "2. 给出 2-4 句简短旁白推进剧情，不要替玩家做决定。\n"
            "3. 若玩家行动对应某个出口，用 move_to 推进；只能推进到下面列出的出口。\n"
            f"   当前可用出口：{legal_exits or '无'}\n"
            "4. 骰子由系统投掷，你不得编造骰子结果。\n"
            "5. 只输出 JSON：{\"narration\":\"旁白\",\"move_to\":\"场景id或null\",\"set_flags\":[\"flag或空数组\"]}。\n"
            f"   已知 flag：{sorted(FLAG_WHITELIST)}\n"
        )
        state_info = (
            f"当前场景：{state.current_scene} 地点：{cur.get('location', '')}\n"
            f"已获得 flag：{sorted(state.flags) or '无'}\n"
        )
        players = "；".join(f"{p.name}({p.character.get('cls', '')})" for p in state.players.values())
        user_msg = f"{context}\n\n{state_info}在场玩家：{players}\n玩家行动：{action}"
        return [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ]

    def _apply_proposal(self, state, obj):
        move_to = obj.get("move_to")
        if move_to:
            cur = state.current()
            for e in cur.get("exits", []):
                if e["to"] == move_to and self._exit_ok(e, state):
                    state.enter(move_to)
                    state.turn += 1
                    break
        for f in obj.get("set_flags", []) or []:
            if f in FLAG_WHITELIST:
                state.set_flag(f)

    def _scripted_narration(self, scene, action):
        exits = "；".join(e["label"] for e in scene.get("exits", []))
        checks = "；".join(f"{c['skill']}检定({c['id']})" for c in scene.get("checks", []))
        hints = []
        if exits:
            hints.append(f"你可以：{exits}")
        if checks:
            hints.append(f"可以尝试：{checks}")
        return f"（DM）{action}——" + " ".join(hints)

    # ---- 小助手建议 ----
    async def suggest(self, state):
        scene = state.current()
        if self.provider is None:
            opts = [e["label"] for e in scene.get("exits", [])]
            opts += [c.get("keywords", [c["id"]])[0] for c in scene.get("checks", [])]
            return opts[:4]
        try:
            sys_msg = (
                "你是玩家的跑团小助手。根据当前场景，给出 3 个简短、具体、可执行的行动建议。"
                "只输出 JSON：{\"options\":[\"...\",\"...\",\"...\"]}，每条不超过 15 字。"
            )
            user_msg = self.store.build_context(state.current_scene, "")
            raw = await self.provider.chat(
                [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
                json_mode=True,
            )
            obj = _parse_json(raw)
            return (obj.get("options") or [])[:4]
        except Exception:
            return [e["label"] for e in scene.get("exits", [])][:4]


def _parse_json(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
        return {}
