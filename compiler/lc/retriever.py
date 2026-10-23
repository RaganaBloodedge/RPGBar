"""`KBRetriever`：把编译好的知识库接进 LangChain 的 Retriever 生态。

它不是"又一个向量检索"——它做的是三路合一，缺一路都会答错：

| 路 | 解决什么 | 不做的后果 |
|---|---|---|
| BM25（FTS5 + 分词） | 精确词命中：技能名、物品名、"拜亚基" | 纯向量会把"拜亚基"和"深潜者"混为一谈 |
| 向量 | 语义近似："我想躲起来" → 潜行 | 纯 BM25 只认字面，玩家换个说法就查不到 |
| **世界时间过滤** | 同一知识点在不同年份的不同版本 | 1925 年的情报被 1930 年的设定污染，DM 会照着旧版判 |

第三路是这套东西和普通 RAG 最大的差别：模组是有时间线的，`block.valid_from/to` 存在
就是为了让"当前是什么时候"参与检索。

`Document.metadata` 里带 `page / section / kind / score / why`，命中的块能直接回原文
（`source_page` 表），而不是"模型说有就有"。
"""
from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field, PrivateAttr

from ..retrieve import KB


class KBRetriever(BaseRetriever):
    """从编译产物（kb.sqlite）检索块。"""

    kb_path: str = Field(description="知识库路径（kb.sqlite）")
    k: int = Field(default=8, description="返回条数")
    kinds: list[str] | None = Field(
        default=None, description="限定五大类：rule / pc / tool / npc / lore"
    )
    year: int | None = Field(
        default=None, description="世界时间；给了就只返回该时间有效的块（超时间的块永远有效）"
    )
    with_vec: bool = Field(default=True, description="是否启用向量召回（缺模型时自动降级为纯 BM25）")

    _kb: Any = PrivateAttr(default=None)

    def _client(self) -> KB:
        if self._kb is None:
            self._kb = KB(self.kb_path, with_vec=self.with_vec)
        return self._kb

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> list[Document]:
        hits = self._client().search(query, k=self.k, kinds=self.kinds, year=self.year)
        return [
            Document(
                page_content=h.text,
                metadata={
                    "block_id": h.block_id,
                    "page": h.page,
                    "title": h.title,
                    "section": h.section,
                    "kind": h.kind,
                    "subtype": h.subtype,
                    "score": round(float(h.score), 4),
                    "why": h.why,
                    "source": self.kb_path,
                },
            )
            for h in hits
        ]

    # 异步版本：内部是同步 sqlite 查询，直接用默认线程池跑，语义与同步版一致
    async def _aget_relevant_documents(
        self, query: str, *, run_manager=None
    ) -> list[Document]:
        import asyncio

        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._get_relevant_documents(query, run_manager=run_manager)
        )

    @property
    def kb(self):
        """暴露底层连接，便于调用方做卡片/流程图等非检索查询。"""
        return self._client()
