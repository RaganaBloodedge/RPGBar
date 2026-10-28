"""剧本切分产物到运行时 Agent 的统一只读适配层。

ScriptStore 提供场景图，KBStore 提供编译器五类容器。这个类只组装静态
证据，不保存本局状态；World/FactEvent 仍是动态事实的唯一来源。
"""
from __future__ import annotations


class ScriptKnowledgeAdapter:
    def __init__(self, store, kb=None):
        self.store = store
        self.kb = kb

    def scene(self, scene_id: str) -> dict:
        return self.store.get_scene(scene_id) or {}

    def action_evidence(self, scene_id: str, flags=None, world=None) -> dict:
        scene = self.scene(scene_id)
        exits = []
        for raw in scene.get("exits", []) or []:
            item = dict(raw)
            target = self.scene(str(item.get("to") or ""))
            item["target_title"] = target.get("title", "")
            item["target_location"] = target.get("location", "")
            item["target_keywords"] = list(target.get("keywords") or [])[:12]
            cond = item.get("condition")
            item["available"] = not cond or cond in set(flags or [])
            exits.append(item)
        evidence = {
            "scene": scene_id,
            "npcs": [dict(n) for n in scene.get("npcs", []) or []
                     if isinstance(n, dict)],
            "checks": [dict(c) for c in scene.get("checks", []) or []
                       if isinstance(c, dict)],
            "exits": exits,
        }
        if world is not None:
            evidence["present_entities"] = world.present()[:24]
            evidence["dynamic_relations"] = world.relations(limit=24)
        return evidence

    def static_relations(self, names, limit: int = 8) -> list[str]:
        if self.kb is None:
            return []
        rows = []
        for name in dict.fromkeys(str(n or "") for n in names):
            if name:
                raw = self.kb.relations(name, limit=4)
                if raw:
                    rows.append(raw)
            if len(rows) >= limit:
                break
        return rows

    def names(self) -> list[str]:
        return self.kb.name_index() if self.kb is not None else []
