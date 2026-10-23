"""把编译好的知识库接成 DM 的工具（只读）。

后端是 `compiler/containers.py` 物化出来的五类容器表：
`rule_entry/rule_flow`、`pc_skill`、`item`、`npc`、`lore_topic` —— DM 查的是**成品**，
不是再拼一遍的原文片段。

为什么只暴露 **两个** 工具（而不是五个）：DM 的工具 schema 每个约 60–90 token，
4B 的上下文只有 6144；工具越多，模型越容易"每个都点一下"。所以按"要问什么"分：

- `kb_lookup(name, kind=auto)`：按名字查规则 / 物品 / 技能 / 怪物档案（有名字的时候用）；
- `kb_lore(query)`：按主题或说法查世界观（玩家用自然语言问设定的时候用）。

知识库从哪来：`RPGBAR_KB` 环境变量 > `config.json` 的 `knowledge.kb` > 自动探测
`data/kb/*/kb.sqlite`（取最新的一个）。找不到就等于这个 DM 没有知识库工具。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .config import RESOURCE_DIR, USER_DIR, load_config

_KIND_LABEL = {"rule": "规则", "item": "物品", "skill": "技能", "npc": "NPC/怪物"}


def _script_path(script_rel: str | None) -> Path | None:
    if not script_rel:
        return None
    for root in (RESOURCE_DIR / "scripts", USER_DIR / "scripts"):
        p = root / script_rel
        if p.exists():
            return p
    return None


def _declared_kb(script_rel: str | None) -> str:
    """剧本 JSON 可以自己声明 `"kb": "coc7e-v3"`（相对 data/kb/ 的目录或文件名）。"""
    p = _script_path(script_rel)
    if p is None or p.suffix.lower() != ".json":
        return ""
    try:
        import json

        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return str((data or {}).get("kb") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def kb_for_script(script_rel: str | None) -> Path | None:
    """**一个剧本对应一个知识库**：剧本 → 它自己编译出来的 kb.sqlite。

    优先级：
    1. `RPGBAR_KB` 环境变量（调试/演示时强制指定）；
    2. 剧本 JSON 里声明的 `"kb"`；
    3. `data/kb/<剧本名>/kb.sqlite`（同名目录）；
    4. `data/kb/<剧本名>-*/kb.sqlite`（同一剧本的多个编译版本，取最新）。

    都找不到就返回 None —— **不再"随便挑一个最新的库"**：拿规则书去配一个无关模组，
    DM 会一本正经地引用不相干的设定，比没有知识库更糟。
    """
    raw = (os.getenv("RPGBAR_KB") or "").strip()
    if raw:
        p = Path(raw)
        if p.is_dir():
            p = p / "kb.sqlite"
        return p if p.exists() else None
    if not script_rel:
        return None
    declared = _declared_kb(script_rel)
    bases = [USER_DIR / "data" / "kb", RESOURCE_DIR / "data" / "kb"]
    if declared:
        for base in bases:
            p = base / declared
            p = p / "kb.sqlite" if p.is_dir() else p
            if p.exists():
                return p
    stem = Path(script_rel).stem
    for base in bases:
        p = base / stem / "kb.sqlite"
        if p.exists():
            return p
        if base.is_dir():
            cands = sorted(
                (d / "kb.sqlite" for d in base.iterdir()
                 if d.is_dir() and d.name.startswith(stem + "-") and (d / "kb.sqlite").exists()),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
            if cands:
                return cands[0]
    return None


class KBStore:
    """一层薄封装：懒加载 compiler.retrieve.KB（它依赖 jieba/embedder，别在启动时加载）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._kb = None
        self._broken = ""
        self._names: list[str] | None = None

    @property
    def kb(self):
        if self._kb is None and not self._broken:
            try:
                from compiler.retrieve import KB

                self._kb = KB(str(self.path), with_vec=False)
            except Exception as exc:  # noqa: BLE001  编译包不在/库坏了：工具直接报不可用
                self._broken = f"{type(exc).__name__}: {exc}"
                print(f"[kb] 知识库不可用（{self.path}）：{self._broken}", file=sys.stderr)
        return self._kb

    @property
    def available(self) -> bool:
        return self.kb is not None

    # ---------------------------------------------------------------- 查询

    def lookup(self, name: str, kind: str = "") -> str:
        kb = self.kb
        if kb is None:
            return "（知识库不可用）"
        name = (name or "").strip()
        if not name:
            return "（请给出要查的名字）"
        kind = (kind or "").strip().lower()
        hits: list[tuple[str, dict]] = []
        want = [kind] if kind in _KIND_LABEL else ["rule", "npc", "item", "skill"]
        for k in want:
            try:
                if k == "rule":
                    card = kb.rule_card(name)
                elif k == "npc":
                    card = kb.npc_card(name)
                elif k == "item":
                    card = kb.item_card(name)
                else:
                    card = kb.skill_card(name)
            except Exception:  # noqa: BLE001  单类查询失败不影响其它类
                continue
            if not card or "error" in card:
                continue
            if k == "rule" and not card.get("steps"):
                # 没有判定步骤的"规则"多半是碎片块（标题恰好等于一个名词）。
                # 只有当别的类都没命中时才把它当规则返回，免得"查侦查"先冒出个空规则。
                hits.append(("rule-fragment", card))
                continue
            hits.append((k, card))
        if not hits:
            # **语义兜底**：名字索引没命中时退回 BM25+向量检索（玩家说的常是口语，
            # 例如"点38左轮" vs 表里的".38左轮手枪"），把最接近的原文给模型，并说明这是模糊命中。
            try:
                near = kb.search(name, k=3)
            except Exception:  # noqa: BLE001
                near = []
            if near:
                lines = [f"没有精确条目「{name}」；最接近的原文如下（模糊命中，引用时注明页码）："]
                for h in near:
                    lines.append(f"· p{h.page} {h.title}：{re.sub(r'\s+', ' ', h.text)[:180]}")
                return "\n".join(lines)
            return f"知识库里没有「{name}」，也没有相近的原文。可以按规则临场裁定，但要说明这是裁定不是书上的条文。"
        # 精确同名的排前面；"规则碎片"永远排最后（真规则/物品/技能优先）
        def _rank(item):
            k, c = item
            label = " ".join(str(c.get(x) or "") for x in ("name", "title")).strip()
            exact = 0 if label == name else (1 if label.startswith(name) else 2)
            return (3 if k == "rule-fragment" else 0, exact, len(label))

        hits.sort(key=_rank)
        return "\n".join(self._render(k, c) for k, c in hits[:2])

    def _render(self, kind: str, card: dict) -> str:
        if kind == "rule-fragment":
            lines = [f"【可能是规则/条目】{card['name']}（p{card.get('page')}，没有判定步骤）"]
            lines.append("  " + re.sub(r"\s+", " ", card.get("text") or "")[:200])
            return "\n".join(lines)
        if kind == "rule":
            lines = [f"【规则】{card['name']}（子类：{card.get('subtype') or '未分类'}，p{card.get('page')}）"]
            if card.get("steps"):
                for s in card["steps"][:8]:
                    lines.append(f"  {s['seq'] + 1}. [{s['kind']}] {s['text']}")
            if card.get("flow"):
                lines.append(f"  （流程图：{len(card['flow']['nodes'])} 节点 / {len(card['flow']['edges'])} 边）")
            if not card.get("steps"):
                lines.append("  " + re.sub(r"\s+", " ", card.get("text") or "")[:220])
            return "\n".join(lines)
        if kind == "npc":
            lines = [f"【{_KIND_LABEL[kind]}】{card['name']}（{card.get('etype')}，p{card.get('page')}，只读）"]
            for f in (card.get("fields") or [])[:10]:
                dice = f" {f.get('dice')}" if f.get("dice") else ""
                lines.append(f"  · {f['key']} = {f['value']}{dice}")
            for a in (card.get("abilities") or [])[:6]:
                lines.append(f"  · 能力 {a['name']}：{re.sub(r'\s+', ' ', a['text'])[:60]}")
            return "\n".join(lines)
        if kind == "item":
            lines = [f"【物品】{card['name']}（p{card.get('page')}）"]
            for f in (card.get("fields") or [])[:10]:
                lines.append(f"  · {f['key']} = {f['value']}")
            if card.get("summary"):
                lines.append("  " + str(card["summary"])[:120])
            return "\n".join(lines)
        # skill
        lines = [f"【技能】{card['name']}（基础值 {card.get('base_value') or '?'}，p{card.get('page')}）"]
        if card.get("description"):
            lines.append("  " + re.sub(r"\s+", " ", card["description"])[:200])
        return "\n".join(lines)

    def _name_index(self) -> list[str]:
        if getattr(self, "_names", None) is None:
            kb = self.kb
            names: set[str] = set()
            if kb is not None:
                for row in kb.con.execute(
                    "SELECT name FROM item UNION SELECT name FROM npc "
                    "UNION SELECT name FROM pc_skill UNION SELECT title FROM rule_entry"
                ):
                    n = (row[0] or "").strip()
                    if len(n) >= 2:
                        names.add(n)
            # 长名优先：这样"深潜者"不会被"深潜"抢先，".38(9mm)左轮"也不会被"左轮"抢先
            self._names = sorted(names, key=len, reverse=True)
        return self._names

    @property
    def rules_d100(self) -> bool:
        from .rules import Rules

        try:
            return Rules.load(self.path).is_d100
        except Exception:  # noqa: BLE001
            return False

    def skill_value(self, name: str) -> tuple[int, str]:
        """规则书里这条技能的基础值（"%"。查不到返回 (0, "")）。"""
        kb = self.kb
        if kb is None or not name:
            return 0, ""
        try:
            card = kb.skill_card(name)
            if "error" in card:
                return 0, ""
            m = re.search(r"(\d{1,3})", str(card.get("base_value") or ""))
            return (int(m.group(1)), "规则书技能表") if m else (0, "")
        except Exception:  # noqa: BLE001
            return 0, ""

    def name_index(self) -> list[str]:
        """库里所有可查的名字（给"玩家提到的东西是否在书里"做比对用）。"""
        return list(self._name_index())

    def related(self, text: str, limit: int = 2) -> str:
        """**确定性注入**：玩家话里出现了库里的名字，就把条目直接摆到 DM 面前。

        为什么不能只靠工具调用：实测同一个模型、同一套提示词，有的轮次会自己调
        kb_lookup，有的轮次完全不调（4B 的工具使用不稳定）。检索这件事不该赌模型自觉 ——
        名字已经在话里了，那就是确定性的信号。
        """
        kb = self.kb
        if kb is None or not text:
            return ""
        hit: list[str] = []
        for name in self._name_index():
            if name in text and not any(name in h for h in hit):
                hit.append(name)
            if len(hit) >= limit:
                break
        if not hit:
            return ""
        return "\n".join(self.lookup(n) for n in hit)

    def lore(self, query: str, k: int = 3) -> str:
        kb = self.kb
        if kb is None:
            return "（知识库不可用）"
        query = (query or "").strip()
        if not query:
            return "（请给出要查的主题或说法）"
        try:
            topics = kb.lore_topics(query, k=k * 3)
        except Exception as exc:  # noqa: BLE001
            return f"（查世界观失败：{type(exc).__name__}）"
        # 主题名命中 > 摘要命中 > 只是被顺带召回；同分按成员数（内容更厚的主题更可能是它）
        def _lscore(t):
            if "fallback" in t:
                return (0, 0)
            label = str(t.get("topic") or "")
            summ = str(t.get("summary") or "")
            return (2 if query in label else (1 if query in summ else 0), int(t.get("members") or 0))

        topics.sort(key=_lscore, reverse=True)
        topics = topics[:k]
        if not topics:
            return f"知识库里没有和「{query}」相关的世界观条目。"
        lines = []
        for t in topics[:k]:
            if "fallback" in t:
                lines.append(f"· p{t['page']} {t['title']}：{t['fallback'][:160]}")
                continue
            lines.append(f"【主题】{t['topic']}（{t['members']} 段原文）")
            if t.get("summary"):
                lines.append("  摘要：" + str(t["summary"])[:160])
            for o in (t.get("originals") or [])[:2]:
                lines.append(f"  原文 p{o['page']}：" + re.sub(r"\s+", " ", o["text"])[:140])
        return "\n".join(lines)


_STORE: dict[str, KBStore] = {}


def kb_store(script_rel: str | None = None) -> KBStore | None:
    """进程内单例（按路径）。找不到库就返回 None：DM 少两个工具，游戏照跑。"""
    p = kb_for_script(script_rel)
    if p is None:
        return None
    key = str(p)
    if key not in _STORE:
        _STORE[key] = KBStore(p)
        print(f"[kb] DM 知识库工具已接入：{p}")
    return _STORE[key]


def build_tools(store: KBStore) -> list:
    """两个只读工具。返回 `server.agents.base.Tool` 列表。"""
    from .agents.base import Tool, _obj, int_prop, str_prop

    def t_lookup(ctx, name="", kind=""):
        return store.lookup(name, kind)

    def t_lore(ctx, query="", k=3):
        try:
            k = max(1, min(int(k or 3), 5))
        except (TypeError, ValueError):
            k = 3
        return store.lore(query, k=k)

    return [
        Tool(
            "kb_lookup",
            "查规则书知识库：按名字查规则（含判定步骤）、物品、技能、怪物/NPC 档案。"
            "玩家提到某个具体名字、或你要确认某条规则/某件装备时用。只读。",
            _obj({
                "name": str_prop("要查的名字，如 侦查 / 深潜者 / 加利尔突击步枪 / 对抗检定"),
                "kind": str_prop("可选：rule / item / skill / npc；省略则自动判类"),
            }, ["name"]),
            t_lookup,
        ),
        Tool(
            "kb_lore",
            "查规则书的世界观设定：按主题或说法检索（返回摘要 + 原文）。"
            "玩家问设定、历史、神话、地点、组织时用。只读。",
            _obj({
                "query": str_prop("主题或玩家说法，如 克苏鲁 / 深潜者的来历"),
                "k": int_prop("返回主题数", 3),
            }, ["query"]),
            t_lore,
        ),
    ]
