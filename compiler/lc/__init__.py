"""LangChain 适配层：把编译内核包成生态里的标准对象。

只做"接口转换"，不做业务判断 —— 判断都在 `compiler/*.py` 里。这一层存在的理由：

- 外部 Agent / 应用想用这个切割器时，直接拿 `Document` / `TextSplitter` / `Retriever` / `Tool`，
  不必读我们的数据结构；
- 我们也能复用 LangChain 的向量库、回调追踪、评测工具，而不是自己造一套。

四件套：

| 文件 | 提供 | 对应内核 |
|---|---|---|
| `loader.py` | PDF → `list[Document]`（一页一个） | `reader` |
| `splitter.py` | `SpecAwareSplitter`（按版式与规范切） | `layout` + `outline` + `spec` |
| `retriever.py` | `KBRetriever`（BM25 + 向量 + 时间过滤） | `retrieve` |
| `tools.py` | 编译与查询工具（给别的 Agent 调） | 全部 |
"""

from .loader import book_document, load_documents
from .retriever import KBRetriever
from .splitter import SpecAwareSplitter
from .tools import build_tools

__all__ = [
    "load_documents",
    "book_document",
    "SpecAwareSplitter",
    "KBRetriever",
    "build_tools",
]
