"""NPC 子系统：人物卡 + 隔离记忆 + 原型兜底。

为什么需要它
------------
剧本外的次要 NPC 原本「没有灵魂」：玩家一跟他们互动，DM 只能临时编，前后不一致。
于是：玩家第一次与某个 NPC 打交道时，用小模型结合剧本背景生成一份**人物小传**（人物卡），
之后所有互动都以这张卡为准 —— 角色就有了一致的性格。

三条设计红线（用户定案）
------------------------
1. **人物卡是房间级共享状态**，落在服务端（主机端），保证所有玩家看到同一个 NPC。
2. **记忆与 DM 隔离**：NPC 的事件日志 / 好感度留在本子系统，**永不进 DM 上下文**；
   DM 只拿到「人物卡 + 当前状态快照」（提炼过的结论，不是流水账）。
3. **模型不给骰点**：`react` 只产出「反应选项 + 权重」，由服务端骰子按权重采样 —— 与
   `roll_check` 同一条铁律。

无模型时走**原型兜底表**（平民 / 暴徒 / 商人 / 卫兵 / 学者 / 权贵），玩法照旧。
"""
from dataclasses import dataclass, field

from . import dice

# 稳定特质轴（0.0 ~ 1.0）：同一张卡长期不变，决策时由「特质 + 情境」推出反应权重。
TRAIT_AXES = ["胆量", "攻击性", "守序", "记仇", "贪财", "好说话"]

# 冲突 / 冒犯类情境下，NPC 的候选反应（兜底原型用同一套标签）。
REACTION_LABELS = ["忍气吞声", "花小钱了事", "呼救报官", "反击", "逃跑"]

# ---------------------------------------------------------------- 原型兜底表

ARCHETYPES = {
    "平民": {
        "keywords": ["平民", "百姓", "村民", "农夫", "摊贩", "伙计", "仆人", "杂役", "老", "妇", "乞丐", "孩童"],
        "traits": {"胆量": 0.30, "攻击性": 0.15, "守序": 0.50, "记仇": 0.20, "贪财": 0.50, "好说话": 0.55},
        "voice": "说话朴实、带点畏缩，遇事习惯先看别人脸色。",
        "goals": ["安稳过日子，别惹事"],
        "fears": ["惹上麻烦", "被官府牵连", "破财"],
        "stance": {"忍气吞声": 0.40, "花小钱了事": 0.35, "呼救报官": 0.10, "反击": 0.10, "逃跑": 0.05},
    },
    "暴徒": {
        "keywords": ["暴徒", "强盗", "匪", "打手", "恶霸", "混混", "亡命", "劫"],
        "traits": {"胆量": 0.75, "攻击性": 0.85, "守序": 0.10, "记仇": 0.75, "贪财": 0.70, "好说话": 0.20},
        "voice": "粗声粗气，三句不离威胁，动辄就要动手。",
        "goals": ["抢到好处", "立威"],
        "fears": ["遇到比自己更狠的人"],
        "stance": {"忍气吞声": 0.05, "花小钱了事": 0.10, "呼救报官": 0.05, "反击": 0.75, "逃跑": 0.05},
    },
    "商人": {
        "keywords": ["商人", "掌柜", "老板", "贩", "商", "摊", "牙行"],
        "traits": {"胆量": 0.40, "攻击性": 0.20, "守序": 0.55, "记仇": 0.35, "贪财": 0.85, "好说话": 0.60},
        "voice": "圆滑客气，话里总留三分余地，先算账再表态。",
        "goals": ["做成买卖", "保住本钱"],
        "fears": ["亏本", "名声受损"],
        "stance": {"忍气吞声": 0.25, "花小钱了事": 0.45, "呼救报官": 0.15, "反击": 0.10, "逃跑": 0.05},
    },
    "卫兵": {
        "keywords": ["卫兵", "守卫", "士兵", "兵", "巡", "捕快", "衙役", "官"],
        "traits": {"胆量": 0.65, "攻击性": 0.55, "守序": 0.85, "记仇": 0.40, "贪财": 0.30, "好说话": 0.35},
        "voice": "公事公办，开口先报规矩，必要时搬出上官。",
        "goals": ["维持秩序", "交差"],
        "fears": ["被上官问责"],
        "stance": {"忍气吞声": 0.10, "花小钱了事": 0.10, "呼救报官": 0.55, "反击": 0.20, "逃跑": 0.05},
    },
    "学者": {
        "keywords": ["学者", "书", "先生", "法师", "术士", "术", "祭", "僧", "道"],
        "traits": {"胆量": 0.35, "攻击性": 0.15, "守序": 0.60, "记仇": 0.25, "贪财": 0.30, "好说话": 0.55},
        "voice": "措辞讲究、爱绕弯，动辄引经据典。",
        "goals": ["求知", "保全名声"],
        "fears": ["卷入暴力", "被当成异端"],
        "stance": {"忍气吞声": 0.30, "花小钱了事": 0.25, "呼救报官": 0.30, "反击": 0.05, "逃跑": 0.10},
    },
    "权贵": {
        "keywords": ["贵族", "老爷", "领主", "官员", "大人", "权贵", "公子", "夫人"],
        "traits": {"胆量": 0.60, "攻击性": 0.45, "守序": 0.55, "记仇": 0.70, "贪财": 0.45, "好说话": 0.25},
        "voice": "居高临下，习惯用命令口吻，很少亲自动手。",
        "goals": ["保住体面", "巩固权势"],
        "fears": ["丢面子", "被扳倒"],
        "stance": {"忍气吞声": 0.05, "花小钱了事": 0.15, "呼救报官": 0.40, "反击": 0.35, "逃跑": 0.05},
    },
}

DEFAULT_ARCHETYPE = "平民"


def infer_archetype(*hints: str) -> str:
    """从身份/风格等文字里猜一个原型（先命中关键词多者）。"""
    blob = " ".join(h for h in hints if h)
    best, best_hits = DEFAULT_ARCHETYPE, 0
    for name, spec in ARCHETYPES.items():
        hits = sum(1 for kw in spec["keywords"] if kw in blob)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def archetype_stance(archetype: str) -> dict:
    spec = ARCHETYPES.get(archetype) or ARCHETYPES[DEFAULT_ARCHETYPE]
    return dict(spec["stance"])


# ---------------------------------------------------------------- 人物卡


def _clamp01(v, default=0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


@dataclass
class PersonaCard:
    """一张 NPC 人物卡（房间级共享，长期稳定）。"""

    id: str
    name: str
    role: str = ""
    archetype: str = DEFAULT_ARCHETYPE
    summary: str = ""
    traits: dict = field(default_factory=dict)
    voice: str = ""
    goals: list = field(default_factory=list)
    fears: list = field(default_factory=list)
    knows: str = ""
    aliases: list = field(default_factory=list)
    scene: str = ""
    from_script: bool = False
    generated: bool = False  # 是否由模型生成（便于排查）

    def norm_name(self, s: str) -> str:
        return (s or "").strip().lower()

    def matches(self, name: str) -> bool:
        n = self.norm_name(name)
        if not n:
            return False
        if n == self.norm_name(self.name):
            return True
        return any(n == self.norm_name(a) for a in self.aliases)

    def archetype_spec(self) -> dict:
        return ARCHETYPES.get(self.archetype) or ARCHETYPES[DEFAULT_ARCHETYPE]

    def stance(self) -> dict:
        return archetype_stance(self.archetype)

    def trait_lines(self) -> str:
        parts = []
        for ax in TRAIT_AXES:
            if ax in self.traits:
                parts.append(f"{ax}{self.traits[ax]:.2f}")
        return "、".join(parts)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "archetype": self.archetype,
            "summary": self.summary,
            "traits": dict(self.traits),
            "voice": self.voice,
            "goals": list(self.goals),
            "fears": list(self.fears),
            "knows": self.knows,
            "aliases": list(self.aliases),
            "scene": self.scene,
            "from_script": self.from_script,
            "generated": self.generated,
        }


@dataclass
class NpcMemory:
    """一个 NPC 的私有记忆（**只在本子系统里**，永不进 DM 上下文）。"""

    npc_id: str
    events: list = field(default_factory=list)  # [{"turn":int,"actor":str,"text":str}]
    affinity: dict = field(default_factory=dict)  # player_name -> float(-1..1)

    def remember(self, actor: str, text: str, turn: int = 0, delta: float = 0.0):
        self.events.append({"turn": turn, "actor": actor, "text": text})
        if len(self.events) > 60:
            self.events = self.events[-60:]
        if actor:
            self.affinity[actor] = max(-1.0, min(1.0, self.affinity.get(actor, 0.0) + delta))

    def snapshot(self) -> dict:
        """给外部的**提炼结论**：只给好感度，绝不给事件流水账（那是本子系统的私产）。"""
        top = sorted(self.affinity.items(), key=lambda kv: -abs(kv[1]))[:3]
        return {"affinity": {k: round(v, 2) for k, v in top if v}}


# 判断一次行动是否会「影响」NPC（有分支后果），决定要不要跑反应采样。
VOLATILE_VERBS = [
    "打", "揍", "攻击", "砍", "刺", "踢", "推", "拉", "拽", "威胁", "恐吓", "骂", "辱", "杀",
    "抢", "偷", "骗", "贿赂", "收买", "给", "送", "扶", "救", "帮", "治疗", "赔", "道歉", "安抚",
    "搜身", "绑", "囚", "拷", "逼", "问", "打听", "询问",
]
HURT_VERBS = ["打", "揍", "攻击", "砍", "刺", "踢", "威胁", "恐吓", "骂", "辱", "杀", "抢", "偷", "骗", "绑", "拷", "逼"]
HELP_VERBS = ["给", "送", "扶", "救", "帮", "治疗", "赔", "道歉", "安抚", "贿赂", "收买"]


class NpcRegistry:
    """NPC 名册：人物卡 + 别名索引（玩家给的外号也归并到这里）。"""

    def __init__(self):
        self.cards: dict[str, PersonaCard] = {}
        self._index: dict[str, str] = {}  # 归一化名字/别名 -> id
        self._seq = 0

    # ---- 基本操作 ----
    def next_id(self) -> str:
        self._seq += 1
        return f"npc{self._seq:02d}"

    def add(self, card: PersonaCard) -> PersonaCard:
        self.cards[card.id] = card
        self._reindex(card)
        return card

    def _reindex(self, card: PersonaCard):
        for key in [card.name] + list(card.aliases):
            if key:
                self._index[str(key).strip().lower()] = card.id

    def get(self, npc_id: str) -> PersonaCard | None:
        return self.cards.get(npc_id)

    def add_alias(self, npc_id: str, alias: str) -> bool:
        card = self.cards.get(npc_id)
        alias = (alias or "").strip()
        if not card or not alias or alias in card.aliases:
            return False
        card.aliases.append(alias)
        self._index[alias.lower()] = card.id
        return True

    # ---- 解析 ----
    def resolve(self, name: str) -> PersonaCard | None:
        """按名字/别名解析；再退一步做包含匹配（容错玩家的叫法）。"""
        if not name:
            return None
        n = name.strip().lower()
        if n in self._index:
            return self.cards[self._index[n]]
        for key, cid in self._index.items():
            if key and (key in n or n in key):
                return self.cards[cid]
        return None

    def detect(self, text: str) -> list:
        """扫一段文本，找出被提及的 NPC（名字/别名以子串出现即算）。"""
        if not text:
            return []
        low = text.lower()
        hit, seen = [], set()
        for key, cid in self._index.items():
            if key and key in low and cid not in seen:
                seen.add(cid)
                hit.append(self.cards[cid])
        return hit

    def __len__(self):
        return len(self.cards)

    def index(self) -> list:
        """给 DM 的「NPC 索引」（只列名字/身份，正文按需取）——控制上下文体量。"""
        return [
            {"id": c.id, "name": c.name, "role": c.role, "aliases": list(c.aliases)}
            for c in self.cards.values()
        ]

    def snapshot(self) -> list:
        return [
            {"id": c.id, "name": c.name, "role": c.role, "archetype": c.archetype,
             "aliases": list(c.aliases), "from_script": c.from_script}
            for c in self.cards.values()
        ]


class NpcService:
    """NPC 子系统的对外门面：生成人设、解析反应、记录记忆。

    - `agent`：主机端小模型（AssistantAgent）。为 None 或没接模型时，一切走原型兜底。
    - `registry`：人物卡名册（房间级）。
    - `memories`：每个 NPC 的私有记忆（**与 DM 隔离**）。
    """

    def __init__(self, store, agent=None):
        self.store = store
        self.agent = agent
        self.registry = NpcRegistry()
        self.memories: dict[str, NpcMemory] = {}
        self.seed_script_npcs()

    def set_agent(self, agent):
        self.agent = agent

    # ---- 名册的常用查询（转发，方便调用方直接用服务对象）----
    def detect(self, text: str) -> list:
        return self.registry.detect(text)

    def resolve(self, name: str) -> PersonaCard | None:
        return self.registry.resolve(name)

    def add_alias(self, npc_id: str, alias: str) -> bool:
        return self.registry.add_alias(npc_id, alias)

    # ---- 剧本原生 NPC：直接从剧本播种（它们本来就有人设，不消耗模型调用）----
    def seed_script_npcs(self):
        for sid, scene in self.store.scenes.items():
            for n in scene.get("npcs", []):
                name = (n.get("name") or "").strip()
                if not name or self.registry.resolve(name):
                    continue
                role = (n.get("role") or "").strip()
                style = (n.get("style") or "").strip()
                arche = infer_archetype(role, style, name)
                card = PersonaCard(
                    id=self.registry.next_id(),
                    name=name,
                    role=role,
                    archetype=arche,
                    summary=style or f"{role or name}",
                    traits=dict(ARCHETYPES[arche]["traits"]),
                    voice=style or ARCHETYPES[arche]["voice"],
                    goals=list(ARCHETYPES[arche]["goals"]),
                    fears=list(ARCHETYPES[arche]["fears"]),
                    knows=(n.get("knows") or "").strip(),
                    scene=sid,
                    from_script=True,
                )
                self.registry.add(card)
                self.memories.setdefault(card.id, NpcMemory(card.id))

    # ---- 人设生成 ----
    async def ensure_card(self, name: str, role: str = "", scene_text: str = "") -> PersonaCard | None:
        """确保某个 NPC 有人物卡：有则返回；没有则用模型生成，失败则原型兜底。"""
        name = (name or "").strip()
        if not name:
            return None
        existing = self.registry.resolve(name)
        if existing:
            return existing

        card = None
        if self.agent is not None and self.agent.provider is not None:
            res = await self.agent.run_task("persona", _PersonaCtx(self, name, role, scene_text))
            data = res.data or {}
            if data.get("name"):
                card = _card_from_data(self.registry, data, scene_text)
                card.generated = True
        if card is None:
            card = _card_from_archetype(self.registry, name, role, scene_text)
        self.registry.add(card)
        self.memories.setdefault(card.id, NpcMemory(card.id))
        return card

    # ---- 反应采样 ----
    def is_consequential(self, text: str) -> bool:
        return any(v in (text or "") for v in VOLATILE_VERBS)

    async def react(self, card: PersonaCard, situation: str) -> dict:
        """产出一组「反应选项 + 权重」，再由服务端骰子采样选定。

        返回：{"options":[{label,weight,...}], "chosen":{...}, "roll":float, "source":str}
        模型**不给骰点**：权重只是区间，采样由 `dice.weighted_pick` 完成。
        """
        options, source = [], "fallback"
        if self.agent is not None and self.agent.provider is not None:
            res = await self.agent.run_task("react_weights", _ReactCtx(self, card, situation))
            options = list((res.data or {}).get("options") or [])
            source = res.source
        if not options:
            options = [{"label": k, "weight": v} for k, v in card.stance().items()]
            source = "fallback"
        weights = [max(0.0, float(o.get("weight", 0))) for o in options]
        idx, roll, total = dice.weighted_pick(weights)
        chosen = options[idx] if 0 <= idx < len(options) else {}
        return {
            "npc": card.name,
            "options": [{"label": o.get("label"), "weight": round(w, 3)} for o, w in zip(options, weights)],
            "chosen": chosen.get("label"),
            "roll": round(roll, 3),
            "total": round(total, 3),
            "source": source,
        }

    # ---- 记忆（只在本子系统内）----
    def remember(self, npc_id: str, actor: str, text: str, turn: int = 0, polarity: float = 0.0):
        mem = self.memories.setdefault(npc_id, NpcMemory(npc_id))
        mem.remember(actor, text, turn=turn, delta=polarity)

    def memory_of(self, npc_id: str) -> NpcMemory:
        return self.memories.setdefault(npc_id, NpcMemory(npc_id))

    # ---- 给 DM 的素材（**不含记忆日志**）----
    def card_brief(self, card: PersonaCard) -> str:
        """人物卡 + 当前状态快照。给 DM 叙事用，绝不含 NPC 的历史流水账。"""
        mem = self.memories.get(card.id)
        mood = ""
        if mem:
            snap = mem.snapshot()
            if snap["affinity"]:
                mood = "　对在场玩家的态度：" + "、".join(
                    f"{k}{'+' if v > 0 else ''}{v}" for k, v in snap["affinity"].items()
                )
        lines = [f"【NPC】{card.name}（{card.role or card.archetype}）"]
        if card.summary:
            lines.append(f"小传：{card.summary}")
        if card.voice:
            lines.append(f"口吻：{card.voice}")
        if card.traits:
            lines.append(f"特质：{card.trait_lines()}")
        if card.goals:
            lines.append("目标：" + "；".join(card.goals))
        if card.fears:
            lines.append("恐惧：" + "；".join(card.fears))
        if card.aliases:
            lines.append("别称：" + "、".join(card.aliases))
        if mood:
            lines.append(mood.strip())
        return "\n".join(lines)

    def card_public(self, card: PersonaCard) -> dict:
        d = card.to_dict()
        mem = self.memories.get(card.id)
        if mem:
            d["status"] = mem.snapshot()
        return d

    def index(self) -> list:
        return self.registry.index()

    def snapshot(self) -> list:
        return self.registry.snapshot()

    def briefs_for(self, cards: list) -> str:
        return "\n\n".join(self.card_brief(c) for c in cards)


# ---------------------------------------------------------------- 上下文与构造


@dataclass
class _PersonaCtx:
    npc: NpcService
    name: str
    role: str = ""
    scene_text: str = ""


@dataclass
class _ReactCtx:
    npc: NpcService
    card: PersonaCard
    situation: str


def _card_from_data(registry: NpcRegistry, data: dict, scene_text: str) -> PersonaCard:
    role = str(data.get("role") or "").strip()
    arche = infer_archetype(role, str(data.get("summary") or ""), str(data.get("voice") or ""))
    traits = {ax: _clamp01((data.get("traits") or {}).get(ax), ARCHETYPES[arche]["traits"][ax]) for ax in TRAIT_AXES}
    aliases = [str(a).strip() for a in (data.get("aliases") or []) if str(a).strip()]
    summary = str(data.get("summary") or "").strip()
    return PersonaCard(
        id=registry.next_id(),
        name=str(data.get("name")).strip(),
        role=role,
        archetype=arche,
        summary=summary,
        traits=traits,
        voice=str(data.get("voice") or "").strip() or ARCHETYPES[arche]["voice"],
        goals=[str(g).strip() for g in (data.get("goals") or []) if str(g).strip()][:4],
        fears=[str(g).strip() for g in (data.get("fears") or []) if str(g).strip()][:4],
        knows=str(data.get("knows") or "").strip(),
        aliases=aliases[:4],
    )


def _card_from_archetype(registry: NpcRegistry, name: str, role: str, scene_text: str) -> PersonaCard:
    arche = infer_archetype(role, name, scene_text)
    spec = ARCHETYPES[arche]
    return PersonaCard(
        id=registry.next_id(),
        name=name,
        role=role,
        archetype=arche,
        summary=f"{role or arche}，{spec['voice']}",
        traits=dict(spec["traits"]),
        voice=spec["voice"],
        goals=list(spec["goals"]),
        fears=list(spec["fears"]),
        generated=False,
    )
