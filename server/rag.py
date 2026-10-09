"""剧本仓库 + RAG 检索。

剧本是强结构化的（章节→场景→节拍），因此检索以「场景」为单位：
用 jieba 分词 + BM25 对场景的可检索文本（标题/地点/关键词/NPC/公开文本）打分，
检索与玩家行动最相关的场景片段，注入 DM 上下文。
"""
import json
import re
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi


def _tokenize(text: str) -> list[str]:
    text = text or ""
    text = re.sub(r"[\s，。！？、；：,.!?;:]+", " ", text)
    tokens = [t for t in jieba.lcut(text) if t.strip()]
    return tokens or [text]


class ScriptStore:
    def __init__(self, path: str | Path):
        with open(path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        self.title = self.data.get("title", "未命名剧本")
        self.system = self.data.get("system", "")
        self.start_scene = self.data.get("start_scene")
        self.scenes = {s["id"]: s for s in self.data.get("scenes", [])}
        # flag 中文描述（剧本可选提供，用于给玩家复述线索）
        self.flag_desc = self.data.get("flags", {}) or {}
        self.flag_ids = self._collect_flags()
        self._corpus, self._ids = self._build_corpus()
        self._bm25 = BM25Okapi(self._corpus)

    def _collect_flags(self) -> set:
        """剧本里出现过的全部 flag（显式声明 + 场景/检定引用），作为 LLM 白名单。"""
        ids = set(self.flag_desc)
        for s in self.scenes.values():
            ids.update(s.get("flags_set", []) or [])
            ids.update(s.get("flags_required", []) or [])
            for c in s.get("checks", []) or []:
                if c.get("success_flag"):
                    ids.add(c["success_flag"])
        return ids

    def flag_label(self, fid: str) -> str:
        """flag 的可读描述，剧本未提供时退回 flag 本身。"""
        return self.flag_desc.get(fid, fid)

    def get_scene(self, scene_id):
        return self.scenes.get(scene_id)

    def _scene_text(self, scene) -> str:
        parts = [
            scene.get("title", ""),
            scene.get("location", ""),
            " ".join(scene.get("keywords", [])),
            " ".join(n.get("name", "") for n in scene.get("npcs", [])),
            scene.get("public_text", ""),
        ]
        return " ".join(p for p in parts if p)

    def _build_corpus(self):
        ids, corpus = [], []
        for sid, s in self.scenes.items():
            ids.append(sid)
            corpus.append(_tokenize(self._scene_text(s)))
        return corpus, ids

    def retrieve(self, current_scene_id: str, query: str, k: int = 3):
        """返回相关场景 id 列表，当前场景永远排第一。"""
        results = []
        if current_scene_id in self.scenes:
            results.append(current_scene_id)
        if query:
            qtoks = _tokenize(query)
            scores = self._bm25.get_scores(qtoks)
            ranked = sorted(
                zip(self._ids, scores), key=lambda x: -x[1]
            )
            for sid, score in ranked:
                if sid != current_scene_id and score > 0:
                    results.append(sid)
                if len(results) >= k + 1:
                    break
        return results

    def scene_brief(self, scene_id: str) -> str:
        s = self.scenes.get(scene_id)
        if not s:
            return "(未知场景)"
        lines = [f"场景id={s['id']} 标题={s['title']} 地点={s['location']}"]
        lines.append(f"公开文本：{s['public_text']}")
        if s.get("dm_notes"):
            lines.append(f"DM内幕：{s['dm_notes']}")
        if s.get("npcs"):
            lines.append("NPC：" + "；".join(f"{n['name']}({n['role']})" for n in s["npcs"]))
        exits = [f"{e['to']}({e['label']})" for e in s.get("exits", [])]
        if exits:
            lines.append("出口：" + "；".join(exits))
        checks = [f"{c['id']}({c['skill']} DC{c['dc']})" for c in s.get("checks", [])]
        if checks:
            lines.append("可触发的检定：" + "；".join(checks))
        return "\n".join(lines)

    def build_context(self, scene_id: str, action: str, k: int = 3) -> str:
        """组装注入 DM 的上下文：当前场景全文 + 检索到的相关场景片段。"""
        parts = [f"【当前场景】\n{self.scene_brief(scene_id)}"]
        related = self.retrieve(scene_id, action, k=k)
        others = [sid for sid in related if sid != scene_id]
        if others:
            parts.append("【检索到的相关剧本片段】")
            for sid in others:
                s = self.scenes[sid]
                parts.append(f"- {s['title']}({sid})：{s['public_text'][:200]}")
        return "\n".join(parts)
