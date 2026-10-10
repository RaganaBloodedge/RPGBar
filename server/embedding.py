"""内置嵌入模型：bge-small-zh-v1.5（ONNX，本地推理，离线可用）。

给「剧情档案」（`server/chronicle.py`）的**混合检索**提供向量侧：
BM25 抓字面，向量抓语义——玩家给 NPC 起了外号、换了说法时，字面未必对得上，
语义向量仍能召回相关桥段。

设计约束：
- **不联网**：模型文件不在版本库里（体积太大），由 `scripts/fetch_embedding.py`
  一次性下载到 `models/bge-small-zh-v1.5/`（打包时一并塞进发行包）。
- **可缺失**：拿不到模型、没装 onnxruntime、文件损坏——都不影响游玩。
  `Embedder.available` 为 False 时，检索自动退化成纯 BM25。
- **只读、线程安全**：onnxruntime 的 `session.run` 本身可重入，这里只加一层缓存。

模型规格：bge-small-zh-v1.5，512 维，CLS 池化 + L2 归一化。
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import RESOURCE_DIR, USER_DIR

MODEL_DIR_NAME = "bge-small-zh-v1.5"
DEFAULT_MAX_LEN = 256
# BGE 系列推荐给「查询」加一句指令前缀（v1.5 影响不大，但加了更稳）
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def _candidate_dirs() -> list[Path]:
    """按优先级找模型目录：环境变量 > 只读资源目录 > 可写目录（打包后 exe 同目录）。"""
    dirs = []
    env = os.environ.get("RPGBAR_EMBED_MODEL_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(RESOURCE_DIR / "models" / MODEL_DIR_NAME)
    dirs.append(USER_DIR / "models" / MODEL_DIR_NAME)
    # 也允许把模型直接摊在 models/ 下
    dirs.append(RESOURCE_DIR / "models")
    dirs.append(USER_DIR / "models")
    return dirs


def _find_files() -> tuple[Path | None, Path | None]:
    """返回 (tokenizer.json, model.onnx|model_quantized.onnx)。"""
    for d in _candidate_dirs():
        if not d.is_dir():
            continue
        tok = d / "tokenizer.json"
        if not tok.exists():
            continue
        for name in ("model.onnx", "model_quantized.onnx", "onnx/model.onnx", "onnx/model_quantized.onnx"):
            m = d / name
            if m.exists():
                return tok, m
    return None, None


class Embedder:
    """bge-small-zh 的本地 ONNX 封装；不可用时静默降级（不抛异常）。"""

    def __init__(self, max_len: int = DEFAULT_MAX_LEN, enabled: bool | None = None):
        self.max_len = max_len
        self.available = False
        self.reason = ""
        self.dim = 0
        self._session = None
        self._tokenizer = None
        self._input_names: list[str] = []

        if enabled is None:
            enabled = (os.environ.get("RPGBAR_EMBED", "on").strip().lower() not in ("off", "0", "false", "no"))
        if not enabled:
            self.reason = "已通过 RPGBAR_EMBED=off 关闭嵌入"
            return

        tok_path, model_path = _find_files()
        if not tok_path or not model_path:
            self.reason = f"未找到模型文件（把 {MODEL_DIR_NAME} 放到 models/ 目录）"
            return
        try:
            import onnxruntime  # noqa: F401  （延迟导入：没装也不该 import 失败）
            from tokenizers import Tokenizer
        except Exception as exc:  # noqa: BLE001
            self.reason = f"缺少依赖 onnxruntime/tokenizers：{exc}"
            return

        try:
            self._tokenizer = Tokenizer.from_file(str(tok_path))
            self._tokenizer.enable_truncation(max_length=max_len)
            opts = onnxruntime.SessionOptions()
            opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2)))
            opts.log_severity_level = 3  # 只报 error，别把 warning 刷进服务器日志
            self._session = onnxruntime.InferenceSession(str(model_path), sess_options=opts)
            self._input_names = [i.name for i in self._session.get_inputs()]
            self.available = True
            self.reason = f"已加载 {model_path.name}"
            self._probe_dim()
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.reason = f"加载失败：{exc}"

    def _probe_dim(self) -> None:
        """跑一次极短的编码，把向量维度探出来（ONNX 的输出维度常是符号，读不到整数）。"""
        try:
            vecs = self._encode_batch(["维度探测"], is_query=False)
            if vecs:
                self.dim = len(vecs[0])
        except Exception:  # noqa: BLE001
            pass

    # ---- 编码 ----
    def encode(self, texts: list[str], is_query: bool = False, batch: int = 16) -> list[list[float]]:
        """把文本编码成 L2 归一化的向量。不可用时返回 []。"""
        if not self.available or not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            out.extend(self._encode_batch(texts[i : i + batch], is_query))
        return out

    def _encode_batch(self, texts: list[str], is_query: bool) -> list[list[float]]:
        import numpy as np

        prefix = QUERY_INSTRUCTION if is_query else ""
        encs = [self._tokenizer.encode(prefix + (t or "")) for t in texts]
        maxlen = max(1, max(len(e.ids) for e in encs))
        ids = np.zeros((len(encs), maxlen), dtype=np.int64)
        mask = np.zeros((len(encs), maxlen), dtype=np.int64)
        for r, e in enumerate(encs):
            n = len(e.ids)
            ids[r, :n] = e.ids
            mask[r, :n] = 1

        feed = {}
        for name in self._input_names:
            if name == "input_ids":
                feed[name] = ids
            elif name == "attention_mask":
                feed[name] = mask
            elif name == "token_type_ids":
                feed[name] = np.zeros_like(ids)
        outs = self._session.run(None, feed)
        # 取模型主输出（(batch, seq, hidden)），CLS 池化 + L2 归一化
        vecs = None
        for o in outs:
            if getattr(o, "ndim", 0) == 3:
                vecs = o
                break
        if vecs is None:
            vecs = outs[0]
        cls = vecs[:, 0, :].astype("float32")
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        cls = cls / norms
        self.dim = int(cls.shape[1])
        return cls.tolist()

    def cosine(self, a: list[float], b: list[float]) -> float:
        """两个已归一化向量的余弦相似度（点积）。"""
        if not a or not b:
            return 0.0
        n = min(len(a), len(b))
        return float(sum(a[i] * b[i] for i in range(n)))

    def status(self) -> dict:
        return {
            "available": self.available,
            "model": MODEL_DIR_NAME,
            "dim": self.dim,
            "reason": self.reason,
        }


_EMBEDDER: Embedder | None = None


def get_embedder(enabled: bool | None = None) -> Embedder:
    """进程级单例：加载一次，之后复用（ONNX 会话初始化不便宜）。"""
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = Embedder(enabled=enabled)
    return _EMBEDDER


def reset_embedder() -> None:
    """测试用：丢掉单例，下次重新探测。"""
    global _EMBEDDER
    _EMBEDDER = None
