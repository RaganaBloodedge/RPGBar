"""给外部 Agent 用的工具集（LangChain `StructuredTool`）。

八个工具，分两类：**编译侧**（把一本书变成知识库）与**运行侧**（DM 跑团时查规则）。

设计上有两条克制的原则：

1. **工具返回原文，不只返回摘要。** 摘要只作为"这是不是你要的"的判断依据，DM 判规则
   必须看原文 —— 摘要会丢条件（"若目标已受伤则……"这种分句一压就没了）。
2. **工具不做推理。** 它只负责"按名字/语义/时间取到那条规则"，判断留给调用它的模型。
   工具里加推理，等于把不可解释的逻辑藏进黑盒，出错时无从复盘。

用法：

```python
from compiler.lc.tools import build_tools
tools = build_tools()                       # 全部八个
agent_tools = build_tools(kb_path="data/kb/coc7e")   # 绑定某个库，检索类工具免传 kb
```

绑定时检索类工具会自动带上 `kb` 默认值，模型不必每次重复写路径 —— 少一个必填参数，
少一类调用错误。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..retrieve import KB
from ..timeline import Timeline


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ------------------------------------------------------------------ 参数契约


class CompileArgs(BaseModel):
    source_path: str = Field(description="剧本/规则书文件路径（PDF）")
    out_dir: str = Field(description="知识库输出目录，会生成 kb.sqlite 等文件")
    limit_pages: int | None = Field(default=None, description="只编译前 N 页，用于试跑")
    with_vec: bool = Field(default=True, description="是否建立向量索引（需要本地嵌入模型）")


class SearchArgs(BaseModel):
    query: str = Field(description="自然语言查询，例如“被怪物吓到要做什么检定”")
    kb: str = Field(default="", description="知识库路径（kb.sqlite）；工具已绑定时可省略")
    k: int = Field(default=8, description="返回条数")
    kinds: list[str] | None = Field(
        default=None, description="限定类别：rule / pc / tool / npc / lore"
    )
    year: int | None = Field(default=None, description="世界时间（如 1925）；只返回该时间有效的块")


class CardArgs(BaseModel):
    name: str = Field(description="实体名，例如“燧发手枪”“拜亚基”“考古学”")
    kb: str = Field(default="", description="知识库路径")
    etype: str | None = Field(
        default=None, description="实体类型：item / skill / monster / npc / spell / rule / lore / attribute"
    )


class FlowArgs(BaseModel):
    rule: str = Field(description="规则名，例如“战斗轮”“理智检定”")
    kb: str = Field(default="", description="知识库路径")


class SectionArgs(BaseModel):
    query: str = Field(default="", description="标题关键字；留空则列出全部一级章节")
    kb: str = Field(default="", description="知识库路径")
    level: int = Field(default=1, description="章节层级，1 为一级章")


class StateArgs(BaseModel):
    campaign: str = Field(description="团（跑团进度）的 id 或名称")
    name: str = Field(default="", description="实体名；留空则返回全团状态表")
    kb: str = Field(default="", description="知识库路径")


class AdvanceArgs(BaseModel):
    campaign: str = Field(description="团 id 或名称")
    note: str = Field(default="", description="本次推进的备注，写进事件流")
    kb: str = Field(default="", description="知识库路径")


# ------------------------------------------------------------------ 实现


def _resolve(kb: str, default_kb: str | None) -> str:
    p = kb or default_kb or ""
    if not p:
        raise ValueError("缺少 kb 参数：请在 build_tools(kb_path=...) 里绑定，或显式传入")
    if not Path(p).exists():
        raise ValueError(f"知识库不存在：{p}")
    return p


def _open(kb: str, default_kb: str | None) -> KB:
    return KB(_resolve(kb, default_kb), with_vec=False)


def build_tools(kb_path: str | None = None) -> list[StructuredTool]:
    """构建工具集。给了 `kb_path` 就把它作为检索类工具的默认库。"""

    def compile_script(
        source_path: str, out_dir: str, limit_pages: int | None = None, with_vec: bool = True
    ) -> str:
        from ..graph import run_compile

        st = run_compile(
            source_path, out_dir, limit_pages=limit_pages, with_vec=with_vec
        )
        rep = st.get("report") or {}
        v = st.get("verdict") or {}
        return _j(
            {
                "ok": bool(v.get("passed")),
                "kb_path": (st.get("store") or {}).get("kb_path"),
                "pages": rep.get("pages"),
                "stats": rep.get("stats"),
                "selfcheck": {
                    "rounds": (rep.get("selfcheck") or {}).get("rounds"),
                    "residual_rate": (rep.get("selfcheck") or {}).get("residual_rate"),
                },
                "spec_compliance": (rep.get("spec") or {}).get("compliance"),
                "verdict": v,
                "trace": [(t.get("node"), t.get("note")) for t in (st.get("trace") or [])],
            }
        )

    def kb_search(query: str, kb: str = "", k: int = 8, kinds: list[str] | None = None,
                  year: int | None = None) -> str:
        client = _open(kb, kb_path)
        try:
            hits = client.search(query, k=k, kinds=kinds, year=year)
            return _j(
                [
                    {
                        "page": h.page,
                        "title": h.title,
                        "kind": h.kind,
                        "subtype": h.subtype,
                        "section": h.section,
                        "score": round(float(h.score), 3),
                        "text": h.text,
                    }
                    for h in hits
                ]
            )
        finally:
            client.close()

    def kb_card(name: str, kb: str = "", etype: str | None = None) -> str:
        client = _open(kb, kb_path)
        try:
            card = client.card(name, etype)
            if not card:
                return _j({"found": False, "name": name})
            return _j({"found": True, **card})
        finally:
            client.close()

    def kb_rule_flow(rule: str, kb: str = "") -> str:
        client = _open(kb, kb_path)
        try:
            f = client.flow(rule)
            return _j({"found": bool(f), "rule": rule, "flow": f})
        finally:
            client.close()

    def kb_sections(query: str = "", kb: str = "", level: int = 1) -> str:
        client = _open(kb, kb_path)
        try:
            sql = "SELECT id,level,title,page_start,page_end,path FROM section WHERE level<=?"
            args: list[Any] = [level]
            if query:
                sql += " AND title LIKE ?"
                args.append(f"%{query}%")
            sql += " ORDER BY page_start, ord"
            return _j([dict(r) for r in client.con.execute(sql, args).fetchall()][:200])
        finally:
            client.close()

    def kb_state(campaign: str, name: str = "", kb: str = "") -> str:
        client = _open(kb, kb_path)
        try:
            tl = Timeline(client.con)
            if name:
                return _j(tl.get_state(campaign, name) or {"found": False, "name": name})
            rows = client.con.execute(
                "SELECT entity_id,status,world_ord FROM entity_state WHERE campaign_id=?", (campaign,)
            ).fetchall()
            return _j([dict(r) for r in rows])
        finally:
            client.close()

    def kb_advance(campaign: str, note: str = "", kb: str = "") -> str:
        client = _open(kb, kb_path)
        try:
            p = Timeline(client.con).advance(campaign, note=note)
            cur = p.get("current") or {}
            return _j(
                {
                    "current": {"page": cur.get("page"), "title": cur.get("title")},
                    "next": [s.get("title") for s in (p.get("next") or [])[:3]],
                    "scene_done": p.get("scene_done"),
                    "scene_total": p.get("scene_total"),
                }
            )
        finally:
            client.close()

    def kb_progress(campaign: str, kb: str = "") -> str:
        client = _open(kb, kb_path)
        try:
            return _j(Timeline(client.con).progress(campaign))
        finally:
            client.close()

    return [
        StructuredTool.from_function(
            func=compile_script,
            name="compile_script",
            description=(
                "把一份剧本/规则书（PDF）编译成可检索的知识库：切分 → 归类 → 抽取 → 建索引。"
                "返回编译是否通过自证（闸门违规、切分自检、规范合规率）。"
            ),
            args_schema=CompileArgs,
        ),
        StructuredTool.from_function(
            func=kb_search,
            name="kb_search",
            description=(
                "在知识库里检索规则/设定原文（BM25 + 向量 + 世界时间过滤）。"
                "返回带页码与出处的原文片段，用于判定规则、回答设定问题。"
            ),
            args_schema=SearchArgs,
        ),
        StructuredTool.from_function(
            func=kb_card,
            name="kb_card",
            description=(
                "按名字取实体卡：物品、技能、怪物/NPC、法术、规则、世界观条目。"
                "返回全部字段（字段是编译时从原文读出来的，不是预设表结构）。"
            ),
            args_schema=CardArgs,
        ),
        StructuredTool.from_function(
            func=kb_rule_flow,
            name="kb_rule_flow",
            description="取某条规则的流程图（节点与边），用于按步骤判定战斗/追逐/理智等流程。",
            args_schema=FlowArgs,
        ),
        StructuredTool.from_function(
            func=kb_sections,
            name="kb_sections",
            description="查章节树：有哪些章、各自页码范围（用于给答案标出处、或决定去哪一章找）。",
            args_schema=SectionArgs,
        ),
        StructuredTool.from_function(
            func=kb_state,
            name="kb_state",
            description=(
                "查实体在当前团里的状态（谁死了、门开了、HP 剩多少）。"
                "这是状态的真相源，覆盖书里的原始定义。"
            ),
            args_schema=StateArgs,
        ),
        StructuredTool.from_function(
            func=kb_advance,
            name="kb_advance",
            description="推进剧情到下一个场景，返回当前场景与接下来三个节点的标题。",
            args_schema=AdvanceArgs,
        ),
        StructuredTool.from_function(
            func=kb_progress,
            name="kb_progress",
            description="查当前团的进度：世界时间、已完成场景数、当前场景。",
            args_schema=AdvanceArgs,
        ),
    ]


def build_toolkit(kb_path: str | None = None) -> dict[str, StructuredTool]:
    """名字 → 工具，便于按需挑选或做路由。"""
    return {t.name: t for t in build_tools(kb_path)}
