"""PDF → LangChain `Document`。一页一个 Document，metadata 里带可回溯的坐标。

为什么保留"页"而不是直接给整本一个 Document：页是原文层的天然分片单位，也是人核对切分
结果时唯一有用的坐标（"p260 切成了 3 块"）。丢了页码，检索命中之后没法回原文。

metadata 全部是 JSON 可序列化的基础类型 —— LangChain 的很多组件（缓存、追踪、向量库）
会把它原样序列化，塞 dataclass 进去迟早炸。
"""
from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document

from .. import layout, reader


def load_documents(
    path: str | Path,
    *,
    book_id: str | None = None,
    limit_pages: int | None = None,
    with_profile: bool = True,
) -> list[Document]:
    """按页加载。`with_profile=True` 时先跑版式画像，页眉页脚带会被正确剔除。

    实例化一次画像大约 70ms（418 页的书），换来的是"页眉不再混进正文"——
    不做的话，每页顶部的书名会变成正文首行，后面切块全歪。这个代价值得。
    """
    book = reader.load(str(path), max_pages=limit_pages)
    prof = layout.profile(book) if with_profile else None
    bid = book_id or Path(path).stem
    docs: list[Document] = []
    for p in book.pages:
        text = p.reading_text()
        if not text.strip():
            continue
        docs.append(
            Document(
                page_content=text,
                metadata={
                    "source": str(path),
                    "book_id": bid,
                    "page": p.no,
                    "chars": len(text),
                    "width": round(p.width, 1),
                    "height": round(p.height, 1),
                    "doc_kind": (prof.doc_kind if prof else "unknown"),
                },
            )
        )
    return docs


def book_document(path: str | Path, *, book_id: str | None = None) -> Document:
    """整本合成一个 Document（`SpecAwareSplitter.split_documents` 的输入）。

    只在"先整本再切"的场合用 —— 切分需要跨页上下文（一条技能可能跨两页），
    逐页切会把跨页条目劈成两半，这正是传统 RAG 切分最常犯的错。
    """
    book = reader.load(str(path))
    text = "\n\n".join(p.reading_text() for p in book.pages)
    return Document(
        page_content=text,
        metadata={
            "source": str(path),
            "book_id": book_id or Path(path).stem,
            "pages": book.page_count,
            "title": book.title,
            "chars": len(text),
        },
    )
