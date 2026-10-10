"""剧情档案（Chronicle）：只增 JSONL + 混合检索（BM25 + 向量 → RRF）。

**为什么要有它**：DM 的上下文装不下一整局的对话，超出就只能总结、遗忘细节，
还会被一堆过程性记忆搅乱。于是把「已经输出过的主线全文」落到本地存储，
之后提到某个关键角色/事件时，再用 `recall_history` 按需检索回来——
DM 不必把历史全背在上下文里，也能「不忘事」。

**谁写谁读**（沿用 NPC 子系统那条红线）：
- 写档案的是**服务端**（权威落盘）。模型既不能改也不能删，只能通过只读工具取用。
- 档案里存的是**已发生的事实**（旁白原文、玩家行动、骰点结论），不是模型的推理草稿。

**检索**：BM25 抓字面、向量抓语义，用 RRF（Reciprocal Rank Fusion）融合。
没有嵌入模型时自动退化成纯 BM25，功能不减、只是召回面窄一点。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

RRF_K = 60


def _tokenize(text: str) -> list[str]:
    text = text or ""
    text = re.sub(r"[\s，。！？、；：,.!?;:]+", " ", text)
    tokens = [t for t in jieba.lcut(text) if t.strip()]
    return tokens or [text]


def entry_id(turn: int, kind: str, scene: str, text: str) -> str:
    raw = f"{turn}|{kind}|{scene}|{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


class Chronicle:
    """一局游戏的剧情档案。线程内使用，外部用锁保护（Room 里串行处理）。"""

    def __init__(self, path: str | Path, embedder=None, title: str = ""):
        self.path = Path(path)
        self.meta_path = self.path.with_suffix(".meta.json")
        self.embedder = embedder  # Embedder 或 None
        self.entries: list[dict] = []
        self.summary: str = ""
        self.summary_upto: int = -1
        self._ids: set[str] = set()
        self._vectors: list[list[float]] = []
        self._bm25: BM25Okapi | None = None
        self._corpus: list[list[str]] = []
        self._title = title
        self._dirty = True
        self.load()

    # ---- 载入 / 落盘 ----
    def load(self) -> bool:
        """从 JSONL 重建内存索引。文件不存在也没关系（返回 False）。"""
        if not self.path.exists():
            return False
        entries: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue  # 坏行跳过，不因一行脏数据丢掉整份档案
                    if isinstance(obj, dict) and obj.get("id"):
                        entries.append(obj)
        except Exception:  # noqa: BLE001
            return False
        self.entries = entries
        self._ids = {e["id"] for e in entries}
        self._rebuild()
        self._load_meta()
        return True

    def _load_meta(self) -> None:
        if not self.meta_path.exists():
            return
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            self.summary = str(meta.get("summary") or "")
            self.summary_upto = int(meta.get("summary_upto", -1))
        except Exception:  # noqa: BLE001
            pass

    def _save_meta(self) -> None:
        try:
            self.meta_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump({"summary": self.summary, "summary_upto": self.summary_upto}, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    def _rebuild(self) -> None:
        self._corpus = [_tokenize(self._index_text(e)) for e in self.entries]
        self._bm25 = BM25Okapi(self._corpus) if self._corpus else None
        self._vectors = self._embed([e.get("text", "") for e in self.entries])
        self._dirty = False

    def _index_text(self, e: dict) -> str:
        parts = [e.get("text", ""), e.get("scene", ""), " ".join(e.get("actors") or []), e.get("kind", "")]
        return " ".join(p for p in parts if p)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if self.embedder is None or not getattr(self.embedder, "available", False) or not texts:
            return []
        try:
            return self.embedder.encode(texts)
        except Exception:  # noqa: BLE001
            return []

    # ---- 写入（只增、幂等）----
    def add(
        self,
        text: str,
        *,
        turn: int = 0,
        kind: str = "narration",
        scene: str = "",
        actors: list[str] | None = None,
        entry: str | None = None,
    ) -> dict | None:
        """追加一条档案。同内容重复写入会被幂等跳过（返回 None）。"""
        text = (text or "").strip()
        if not text:
            return None
        eid = entry or entry_id(int(turn), kind, scene, text)
        if eid in self._ids:
            return None
        obj = {
            "id": eid,
            "turn": int(turn),
            "kind": kind,
            "scene": scene,
            "actors": list(actors or []),
            "text": text,
            "ts": round(time.time(), 3),
        }
        self.entries.append(obj)
        self._ids.add(eid)
        self._corpus.append(_tokenize(self._index_text(obj)))
        self._bm25 = BM25Okapi(self._corpus) if self._corpus else None
        vecs = self._embed([text])
        self._vectors.append(vecs[0] if vecs else [])
        self._append_line(obj)
        return obj

    def _append_line(self, obj: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # ---- 读取 ----
    def recent(self, n: int = 6) -> list[dict]:
        return self.entries[-max(0, n):] if n else []

    def __len__(self) -> int:
        return len(self.entries)

    def actors(self) -> list[str]:
        seen: dict[str, int] = {}
        for e in self.entries:
            for a in e.get("actors") or []:
                seen[a] = seen.get(a, 0) + 1
        return [a for a, _ in sorted(seen.items(), key=lambda x: -x[1])]

    def search(self, query: str, k: int = 4, actor: str | None = None) -> list[dict]:
        """混合检索：BM25 字面 + 向量语义，RRF 融合后取前 k 条。"""
        if not self.entries:
            return []
        k = max(1, min(int(k or 4), 12))
        pool = range(len(self.entries))
        if actor:
            hit = [i for i, e in enumerate(self.entries) if self._mentions(e, actor)]
            if len(hit) >= k:
                pool = hit
        pool = list(pool)

        ranks: dict[int, float] = {}
        for order in (self._bm25_rank(query, pool), self._vector_rank(query, pool)):
            for rank, idx in enumerate(order):
                ranks[idx] = ranks.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        if not ranks:  # 查询词太生僻 → 退化成「最近若干条」
            return [self._view(e) for e in self.recent(k)]
        best = sorted(ranks.items(), key=lambda x: -x[1])[:k]
        out = []
        for idx, score in best:
            v = self._view(self.entries[idx])
            v["score"] = round(score, 5)
            out.append(v)
        return out

    def _mentions(self, e: dict, actor: str) -> bool:
        if actor in (e.get("actors") or []):
            return True
        return actor in (e.get("text") or "")

    def _bm25_rank(self, query: str, pool: list[int]) -> list[int]:
        if self._bm25 is None or not query:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        order = sorted(pool, key=lambda i: -float(scores[i]) if i < len(scores) else 0.0)
        return [i for i in order if i < len(scores) and scores[i] > 0]

    def _vector_rank(self, query: str, pool: list[int]) -> list[int]:
        if not self._vectors or not query or self.embedder is None:
            return []
        try:
            qv = self.embedder.encode([query], is_query=True)
        except Exception:  # noqa: BLE001
            return []
        if not qv:
            return []
        q = qv[0]
        scored = [(i, self.embedder.cosine(q, self._vectors[i])) for i in pool if i < len(self._vectors) and self._vectors[i]]
        scored.sort(key=lambda x: -x[1])
        return [i for i, s in scored if s > 0.2]

    def _view(self, e: dict) -> dict:
        return {
            "id": e.get("id"),
            "turn": e.get("turn"),
            "kind": e.get("kind"),
            "scene": e.get("scene"),
            "actors": e.get("actors") or [],
            "text": e.get("text"),
        }

    # ---- 摘要（滚动主线）----
    def set_summary(self, text: str, upto_turn: int = -1) -> None:
        self.summary = (text or "").strip()
        self.summary_upto = int(upto_turn)
        self._save_meta()

    def digest(self, n: int = 6, width: int = 160) -> str:
        """给提示词用的廉价「热层」文本：滚动摘要 + 最近 n 条（不需模型）。"""
        lines = []
        if self.summary:
            lines.append(f"【此前剧情概要】{self.summary}")
        rec = self.recent(n)
        if rec:
            lines.append("【最近发生】")
            for e in rec:
                who = "、".join(e.get("actors") or [])
                tag = f"{who}：" if who else ""
                txt = (e.get("text") or "").strip().replace("\n", " ")
                if len(txt) > width:
                    txt = txt[:width] + "…"
                lines.append(f"- {tag}{txt}")
        return "\n".join(lines)

    def snapshot(self) -> dict:
        return {"entries": len(self.entries), "summary": self.summary, "recent": len(self.recent(6))}

    def stats(self) -> dict:
        return {
            "entries": len(self.entries),
            "actors": self.actors()[:20],
            "has_summary": bool(self.summary),
            "embedder": getattr(self.embedder, "available", False),
        }
