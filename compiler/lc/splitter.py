"""`SpecAwareSplitter`：一个**按规范切、而不是按字符数切**的 TextSplitter。

为什么要自己写一个，而不是用 `RecursiveCharacterTextSplitter`：

    规则书的一页长这样
        ┌──────────────────────────────┐
        │ 6.2 常规接触战斗              │   ← 这是标题，不能算正文
        │ 战斗轮按 DEX 顺序进行。每轮…   │   ← 这是规则正文
        │ 男装                          │
        │ 精纺毛料礼服，$17.95          │   ← 这是清单行，一行一个商品
        └──────────────────────────────┘

按字符数切（无论怎么调 chunk_size / 分隔符），都会在"战斗轮按 DEX"这种句子中间断开，
或者把 20 个商品粘成一块。这两种错都无法靠调参解决 —— 因为**边界不在字符层面，
而在版式层面**（行距、缩进、表格网格、字号）。所以这个 splitter 的判据是版式与规范，
不是字符数。

两条路径，说得明白些：

- `split_documents()`：输入 Document 的 metadata 带 `source`（本地 PDF），走**版式路径** ——
  重跑 reader/layout/outline，分栏、找条目头、认清单行，切出来的块可以精确回溯到页码。
  这是推荐路径。
- `split_text()`：只有纯字符串、没有版式信息时的**降级路径** —— 按规范里能靠文字判定的
  边界切（清单行、条目头、空行、句末），比字符切分好，但不如版式路径准。

接口上仍然满足 LangChain 的 `TextSplitter` 契约，所以它能塞进任何现成的 pipeline。
"""
from __future__ import annotations

import re
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import TextSplitter

from .. import layout, outline, reader, spec


class SpecAwareSplitter(TextSplitter):
    """按正文规范切块。`max_chars` 是**兜底上限**，不是目标尺寸。"""

    def __init__(
        self,
        max_chars: int = 520,
        *,
        book_id: str | None = None,
        drop_copyright: bool = True,
        **kwargs,
    ):
        # TextSplitter 的 chunk_size 语义与我们不同（它按字符，我们按版式），
        # 这里只用来满足父类契约。
        super().__init__(chunk_size=max_chars, chunk_overlap=0, **kwargs)
        self.max_chars = max_chars
        self.book_id = book_id
        self.drop_copyright = drop_copyright

    # ---------------------------------------------------------------- 版式路径

    def split_documents(self, documents: list[Document]) -> list[Document]:
        """有版式信息就走这里。**一次只处理一本**，因为切分要全局版式画像。"""
        out: list[Document] = []
        for doc in documents:
            src = (doc.metadata or {}).get("source") or ""
            if not src or not Path(src).exists():
                # 没有本地版式信息 → 降级为纯文本切分，metadata 原样带过去
                for c in self.split_text(doc.page_content):
                    out.append(Document(page_content=c, metadata=dict(doc.metadata or {})))
                continue
            out.extend(self._split_by_layout(src, doc.metadata or {}))
        return out

    def _split_by_layout(self, path: str, meta: dict) -> list[Document]:
        from .. import gate

        book = reader.load(path)
        prof = layout.profile(book)
        bid = self.book_id or meta.get("book_id") or Path(path).stem
        sections = outline.build_sections(book, prof, bid)

        docs: list[Document] = []
        for sec in sections:
            if self.drop_copyright and not gate.judge_section(sec.title).keep:
                continue
            for b in outline.split_blocks(book, sec, prof, max_chars=self.max_chars):
                if self.drop_copyright and not gate.judge_block(b.text, b.title).keep:
                    continue
                docs.append(
                    Document(
                        page_content=b.text,
                        metadata={
                            "source": path,
                            "book_id": bid,
                            "block_id": b.id,
                            "page": b.page,
                            "page_end": b.page_end,
                            "section": sec.title,
                            "title": b.title,
                            "kind": b.kind or "",
                            "subtype": b.subtype or "",
                            "chars": len(b.text),
                            "is_table": bool(b.meta.get("is_table")),
                            "list_item": bool(b.meta.get("list_item")),
                        },
                    )
                )
        return docs

    # ---------------------------------------------------------------- 降级路径

    def split_text(self, text: str) -> list[str]:
        """纯文本降级切分：清单行 → 条目头 → 空行段落 → 句末兜底。

        判据顺序刻意从"最硬的版式特征"到"最软的句读"：清单行（一行一物）比空行段落更硬，
        空行段落又比句子边界更硬。违反这个顺序，就会把商品清单当成段落合并。
        """
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            return []

        chunks: list[str] = []
        buf: list[str] = []

        def flush() -> None:
            nonlocal buf
            body = "\n".join(buf).strip()
            if body:
                chunks.extend(self._enforce_limit(body))
            buf = []

        lines = text.split("\n")
        list_run: list[str] = []

        def flush_list() -> None:
            nonlocal list_run
            if list_run:
                chunks.extend(list_run)  # 清单行一行一块：这样"查某个商品"才可能命中
                list_run = []

        for ln in lines:
            s = ln.strip()
            if not s:
                flush_list()
                flush()
                continue
            if outline.parse_list_line(s):
                flush()
                list_run.append(s)
                continue
            flush_list()
            # 条目头：短、不像句子、后面跟着更长的行 —— 这是"一条规则/一只怪"的起点
            if self._looks_like_head(s):
                flush()
            buf.append(s)
        flush_list()
        flush()

        return [c for c in chunks if c.strip()]

    @staticmethod
    def _looks_like_head(s: str) -> bool:
        """条目头判据：短、**没有句末标点**、不像一句完整的话。

        实测踩过的坑：`战斗轮按 DEX 顺序进行。每轮大约十秒。若有人突袭，则……` 正好 30 字，
        只看长度会被当成标题，于是"战斗轮"和它的正文被切成两块。判据里加上"含句号就不是标题"
        才稳 —— 标题不会自带句号。
        """
        if not (2 <= len(s) <= 30):
            return False
        if s[-1] in "。！？，、；：…":
            return False
        if any(ch in s for ch in "。！？"):
            return False
        return not re.search(r"\d+[.、]\s*\S", s) or s.startswith(("第", "附录"))

    def _enforce_limit(self, body: str) -> list[str]:
        """超上限才硬切 —— 且在句末切，不在字中间切。"""
        if len(body) <= self.max_chars:
            return [body]
        parts, cur = [], ""
        for sent in re.split(r"(?<=[。！？；])", body):
            if not sent:
                continue
            if len(cur) + len(sent) > self.max_chars and cur:
                parts.append(cur.strip())
                cur = sent
            else:
                cur += sent
        if cur.strip():
            parts.append(cur.strip())
        # 单句就超长的极端情况：只能按上限硬切，但至少标出来
        out: list[str] = []
        for p in parts:
            while len(p) > self.max_chars:
                out.append(p[: self.max_chars])
                p = p[self.max_chars :]
            if p.strip():
                out.append(p)
        return out

    # ---------------------------------------------------------------- 自检

    def spec_boundaries(self) -> list[str]:
        """这份 splitter 遵循的边界规则（给报告/调试看，避免"它到底按什么切的"说不清）。"""
        return [
            f"`{k}`：{spec.SPEC[k].boundary}" for k in spec.KINDS
        ] + [
            "清单行（名称，价格）→ 一行一块",
            "条目头（短行 + 后接正文）→ 一条一块",
            f"兜底上限 {self.max_chars} 字，超限在句末切",
        ]
