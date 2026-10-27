"""统一的玩法入口契约。

玩家选择的是一套玩法定义，而不是一个孤立文件名。固定冒险和规则驱动沙盒
仍然可以使用同一个 ScriptStore，但它们通过这里声明自己的内容来源、规则来源
和开局生成方式，避免运行时到处判断文件名或重复推导。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RuleBinding:
    """规则来源的公开描述；具体 Rules 对象仍由房间按此绑定加载。"""

    ref: str = "generic_d20"
    profile: str = "d20 + 加值 vs DC"
    knowledge_ref: str = ""


@dataclass(frozen=True)
class ScriptKnowledge:
    """切分器产出的静态内容摘要，不承载本局运行状态。"""

    source: str
    title: str
    structured: bool
    scenes: int
    flags: int
    chunks: int
    knowledge_ref: str = ""


@dataclass(frozen=True)
class PlayDefinition:
    """大厅、房间和 Agent 共享的玩法入口。"""

    play_id: str
    title: str
    mode: str
    mode_label: str
    script_rel: str
    description: str = ""
    generation_policy: str = "prebuilt_scene_graph"
    content_ref: str = ""
    rules: RuleBinding = field(default_factory=RuleBinding)
    knowledge: ScriptKnowledge | None = None
    players: dict = field(default_factory=dict)

    @property
    def is_sandbox(self) -> bool:
        return self.mode == "sandbox"

    @classmethod
    def from_store(cls, script_rel: str, store) -> "PlayDefinition":
        data = getattr(store, "data", None) or {}
        sandbox = bool(data.get("sandbox"))
        mode = "sandbox" if sandbox else "scenario"
        title = str(getattr(store, "title", "") or Path(script_rel).stem)
        # 规则描述在这里读取一次，列表、房间和日志都使用同一来源。
        ref = str(data.get("rules") or data.get("rule_profile") or "")
        kb_ref = str(data.get("kb") or "")
        profile = "通用 d20 + DC"
        try:
            from .kb_tools import kb_for_script
            from .rules import Rules

            kb_path = kb_for_script(script_rel)
            rules = Rules.load(kb_path)
            profile = rules.describe()
            if kb_path and not kb_ref:
                kb_ref = str(Path(kb_path).parent.name)
            if rules.is_d100 and not ref:
                ref = "d100"
        except Exception:  # noqa: BLE001
            pass
        ref = ref or "generic_d20"
        knowledge = ScriptKnowledge(
            source=script_rel,
            title=title,
            structured=bool(getattr(getattr(store, "doc", None), "structured", False)),
            scenes=len(getattr(store, "scenes", {}) or {}),
            flags=len(getattr(store, "flag_ids", set()) or set()),
            chunks=len(getattr(store, "chunk_list", []) or []),
            knowledge_ref=kb_ref,
        )
        return cls(
            play_id=Path(script_rel).stem or title,
            title=title,
            mode=mode,
            mode_label="沙盒玩法" if sandbox else "固定冒险",
            script_rel=script_rel,
            description=str(data.get("premise") or "")[:180],
            generation_policy="session_zero" if sandbox else "prebuilt_scene_graph",
            content_ref="" if sandbox else script_rel,
            rules=RuleBinding(ref=ref, profile=profile, knowledge_ref=kb_ref),
            knowledge=knowledge,
            players=dict(getattr(store, "players", {}) or {}),
        )

    def to_public(self) -> dict:
        """返回可给大厅/房间使用的公开信息，不暴露本机路径。"""
        knowledge = self.knowledge
        return {
            "id": self.play_id,
            "path": self.script_rel,
            "title": self.title,
            "mode": self.mode,
            "mode_label": self.mode_label,
            "description": self.description,
            "generation_policy": self.generation_policy,
            "rules": self.rules.profile,
            "rules_ref": self.rules.ref,
            "knowledge_ref": self.rules.knowledge_ref,
            "structured": bool(knowledge and knowledge.structured),
            "scenes": knowledge.scenes if knowledge else 0,
            "flags": knowledge.flags if knowledge else 0,
            "chunks": knowledge.chunks if knowledge else 0,
            "players": dict(self.players),
        }
