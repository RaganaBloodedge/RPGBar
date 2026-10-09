"""玩家私有小助手（小模型）：AdvisorAgent。

职责边界很清楚——**只读**。它的工具集里没有任何能改游戏状态的东西，
所以「小助手不会替玩家改剧情」不是靠提示词祈求，而是靠它拿不到写工具。

- 只读工具：read_scene / list_valid_actions / lookup_script
- finish(options)：输出 2-4 条建议

这条通道是给「玩家自己机器上的 3B/4B 模型」准备的：把小助手指向
`http://127.0.0.1:8080/v1` 之类的本地地址即可，算力花在玩家自己身上。
"""
from dataclasses import dataclass

from .base import Agent, AgentResult, Tool, _obj, int_prop, str_prop

ADVISOR_JSON_HINT = {
    "role": "user",
    "content": (
        "该模型无法使用工具。请只输出一个 JSON 对象，不要任何解释文字：\n"
        '{"options":["建议1","建议2","建议3"]}\n'
        "每条建议不超过 15 字，必须是玩家当下可以做的具体行动。"
    ),
}


@dataclass
class AdvisorContext:
    state: object
    player_name: str = "冒险者"


class AdvisorAgent(Agent):
    name = "advisor"
    max_steps = 4
    json_hint = ADVISOR_JSON_HINT

    def __init__(self, store, provider=None):
        super().__init__(provider)
        self.store = store

    # 第一层：System prompt —— 只写小助手自己的职责（与具体剧本无关）。
    def system_prompt(self, ctx) -> str:
        return (
            "你是玩家在文字跑团里的私人小助手，不是主持人（DM）。\n"
            "【你的职责】\n"
            "1. 看着当前场景，给出 3 条玩家**当下就能做**的具体行动建议。\n"
            "2. 建议要短、要以动词开头（例如「检查石门」「合力抬开石闩」）。\n"
            "【工作方式】\n"
            "- 先调用 list_valid_actions 看有哪些合法选项，必要时用 read_scene / lookup_script 弄清场景。\n"
            "- 最后调用 finish(options) 输出建议列表。\n"
            "【硬性边界】\n"
            "- 你只能读，不能改剧情；不替玩家做决定，只给选项。\n"
            "- 每条建议不超过 15 字，且必须是当前场景真实可做的行动。\n"
        )

    # 第二层：次级 prompt —— 剧本背景与约束（公开版，不含内幕与场景索引，防剧透）。
    def secondary_prompt(self, ctx) -> str:
        return self.store.script_prompt_public

    def build_user_message(self, ctx, user_input: str) -> str:
        state = ctx.state
        cur = state.current()
        return (
            f"当前场景：{cur.get('title', '')}（{cur.get('location', '')}）\n"
            f"地点描述：{cur.get('public_text', '')}\n"
            f"队伍已获线索：{'；'.join(self.store.flag_label(f) for f in sorted(state.flags)) or '无'}\n"
            f"玩家：{ctx.player_name}\n"
            "请给出 3 条行动建议。"
        )

    # ---- 只读工具 ----
    def build_tools(self, ctx) -> list:
        return [
            Tool(
                "read_scene",
                "读取当前场景的公开描述（不含只有 DM 知道的内幕）。只读。",
                _obj({"scene_id": str_prop("场景 id，省略则当前场景")}),
                self._t_read_scene,
            ),
            Tool(
                "list_valid_actions",
                "列出当前场景所有合法的行动选项：出口（可移动）与可尝试的检定。只读。",
                _obj(),
                self._t_list_valid_actions,
            ),
            Tool(
                "lookup_script",
                "在剧本里检索与关键词最相关的片段。只读。",
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

    def _t_read_scene(self, ctx, scene_id=""):
        sid = (scene_id or "").strip() or ctx.state.current_scene
        s = self.store.get_scene(sid)
        if not s:
            return {"error": f"未知场景：{sid}"}
        # 注意：不下发 dm_notes（那是 DM 的内幕），小助手不该看到
        return {"id": s["id"], "title": s["title"], "location": s["location"], "public_text": s["public_text"]}

    def _t_list_valid_actions(self, ctx):
        scene = ctx.state.current()
        exits = [
            {"option": e["label"], "kind": "move", "target": e["to"]}
            for e in scene.get("exits", [])
        ]
        checks = [
            {"option": (c.get("keywords") or [c["id"]])[0], "kind": "attempt", "check_id": c["id"]}
            for c in scene.get("checks", [])
        ]
        return {"exits": exits, "attempts": checks}

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

    # ---- 降级 ----
    def parse_final(self, text, ctx):
        obj = _parse_json(text)
        return obj or None

    def fallback_text(self, ctx, user_input: str) -> str:
        return ""

    def fallback_options(self, ctx) -> list:
        """无模型时的建议：直接取剧本里的出口与检定关键词。"""
        scene = ctx.state.current()
        opts = [e["label"] for e in scene.get("exits", [])]
        opts += [(c.get("keywords") or [c["id"]])[0] for c in scene.get("checks", [])]
        return opts[:4]

    async def suggest(self, ctx) -> AgentResult:
        """对外入口：返回 AgentResult，建议列表在 data['options']。"""
        if self.provider is None:
            return AgentResult(
                data={"options": self.fallback_options(ctx)},
                source="scripted",
                stopped="no_provider",
            )
        res = await self.run(ctx, "给出行动建议")
        opts = list(res.data.get("options") or [])
        if not opts:
            # 模型没按协议给列表 → 兜底，但保留 trace
            opts = self.fallback_options(ctx)
            if not res.text:
                res.source = "degraded"
        res.data = {"options": opts[:4]}
        return res


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
