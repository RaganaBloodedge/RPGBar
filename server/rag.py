"""剧本仓库 + RAG 检索。

剧本经 `script_loader` 读入并切片：读取文件 → 自动切片 → 生成「次级 prompt」。
检索以「场景」为单位：用 jieba 分词 + BM25 对场景的可检索文本
（标题/地点/关键词/NPC/公开文本）打分，检索与玩家行动最相关的场景片段注入 DM 上下文。
"""
import re
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

from .script_loader import build_script_prompt, parse_script, read_script_file


def _tokenize(text: str) -> list[str]:
    text = text or ""
    text = re.sub(r"[\s，。！？、；：,.!?;:]+", " ", text)
    tokens = [t for t in jieba.lcut(text) if t.strip()]
    return tokens or [text]


class ScriptStore:
    """一份已经读入、切片、并生成了次级 prompt 的剧本。

    - `system`：剧本自带的世界观/语气（现在只进次级 prompt，不再混进 System prompt）
    - `chunks` / `chunk_list`：自动切片的结果
    - `script_prompt`：给 DM 的次级 prompt（完整版）
    - `script_prompt_public`：给小助手的次级 prompt（公开版，无内幕、无索引）
    """

    def __init__(self, path: str | Path, label: str | None = None):
        self.source = label or str(path)
        doc = read_script_file(path)
        if label:
            doc.source = label  # 次级 prompt 里显示相对路径，别把本机绝对路径塞进提示词
        self._init(doc)

    @classmethod
    def from_content(cls, raw: str, name: str = "<inline>") -> "ScriptStore":
        """从内存里的文本构建（供「粘贴剧本内容」用）。"""
        obj = cls.__new__(cls)
        obj.source = name
        obj._init(parse_script(raw, source=name, name=name))
        return obj

    def _init(self, doc):
        self.doc = doc
        self.data = doc.data
        self.title = doc.title
        self.system = doc.system
        self.premise = doc.premise
        self.start_scene = doc.start_scene
        self.fmt = doc.fmt
        self.warnings = list(doc.warnings)
        self.scenes = {s["id"]: s for s in self.data.get("scenes", [])}
        # flag 中文描述（剧本可选提供，用于给玩家复述线索）
        self.flag_desc = self.data.get("flags", {}) or {}
        self.flag_ids = self._collect_flags()
        # 自动切片：Chunk 列表 + 便于按 id 取用的字典
        self.chunk_list = list(doc.chunks)
        self.chunks = {c.id: c for c in doc.chunks}
        # 次级 prompt：排在 System prompt 之后的那一层
        self.script_prompt = build_script_prompt(doc, "dm")
        self.script_prompt_public = build_script_prompt(doc, "advisor")
        self._corpus, self._ids = self._build_corpus()
        self._bm25 = BM25Okapi(self._corpus) if self._corpus else None

    def inspect(self, preview: int = 120) -> dict:
        """给「剧本」界面看的自检信息：切片 + 两份次级 prompt + 告警。"""
        out = self.doc.to_dict(preview)
        out.update(
            {
                "premise": self.premise,
                "system": self.system,
                "script_prompt": self.script_prompt,
                "script_prompt_public": self.script_prompt_public,
            }
        )
        return out

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
        if query and self._bm25 is not None:
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
            lines.append(
                "NPC：" + "；".join(f"{n.get('name', '?')}({n.get('role', '')})" for n in s["npcs"])
            )
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
