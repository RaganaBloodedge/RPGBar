"""本地向量：bge-small-zh-v1.5（ONNX），拿不到就静默退化。

为什么向量是"可选"而不是"必需"：规则查询的主路径是 **精确名索引 + BM25**（"拜亚基"
"护甲""消耗"这些词，字面就能对上）；向量只在玩家换了说法时补位（"那个会飞的马" → 拜亚基）。
把它做成硬依赖，会让整个编译器在没有模型文件的机器上直接跑不起来 —— 不值。
"""
from __future__ import annotations

from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class Embedder:
    def __init__(self, max_len: int = 256):
        self.available = False
        self.reason = ""
        self.dim = 0
        self._tok = None
        self._sess = None
        self._inputs: list[str] = []
        try:
            import onnxruntime  # noqa: F401
            from tokenizers import Tokenizer
        except Exception as exc:  # noqa: BLE001
            self.reason = f"缺 onnxruntime/tokenizers：{exc}"
            return
        tok_p = MODEL_DIR / "tokenizer.json"
        model_p = MODEL_DIR / "model_quantized.onnx"
        if not tok_p.exists() or not model_p.exists():
            self.reason = f"没找到模型：{MODEL_DIR}"
            return
        try:
            import onnxruntime

            self._tok = Tokenizer.from_file(str(tok_p))
            self._tok.enable_truncation(max_length=max_len)
            opt = onnxruntime.SessionOptions()
            opt.log_severity_level = 3
            self._sess = onnxruntime.InferenceSession(str(model_p), sess_options=opt)
            self._inputs = [i.name for i in self._sess.get_inputs()]
            self.available = True
            v = self.encode(["维度"], is_query=False)
            self.dim = int(v.shape[1]) if v is not None and len(v) else 0
            self.reason = "ok"
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.reason = f"加载失败：{exc}"

    def encode(self, texts: list[str], is_query: bool = False):
        if not self.available or not texts:
            return None
        import numpy as np

        encs = [self._tok.encode((QUERY_PREFIX if is_query else "") + (t or "")) for t in texts]
        n = max(1, max(len(e.ids) for e in encs))
        ids = np.zeros((len(encs), n), dtype=np.int64)
        mask = np.zeros((len(encs), n), dtype=np.int64)
        for i, e in enumerate(encs):
            k = len(e.ids)
            ids[i, :k] = e.ids
            mask[i, :k] = 1
        feed = {}
        for name in self._inputs:
            if name == "input_ids":
                feed[name] = ids
            elif name == "attention_mask":
                feed[name] = mask
            elif name == "token_type_ids":
                feed[name] = np.zeros_like(ids)
        out = self._sess.run(None, feed)[0]  # (B, T, H) 或 (B, H)
        if out.ndim == 3:
            vec = out[:, 0, :]  # CLS 池化（bge 系列的标准做法）
        else:
            vec = out
        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        return vec / np.maximum(norm, 1e-8)
