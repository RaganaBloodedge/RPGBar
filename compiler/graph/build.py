"""把节点连成图，并给出两个稳定入口：`run_compile` / `resume_compile`。

图长这样（条件边标注在箭头上）：

```
read ──(源文件不存在)──────────────────────────────────────────► END
  └─ profile ─ outline ─ gate ─┬─ split ─ selfcheck ⟲ ─ classify ─┬─ llm_review ─┐
                               │      （未收敛就回自己）           │（仅当启用）    │
                               └──────────────────────────────────┴──────────────► store
                                                    store → extract → graph → index → report → END
```

两个工程决定：

- **入口只做一次 invoke，不暴露 12 个函数让调用方按顺序调。** 顺序是图的属性，不是调用方的义务；
  调用方只要给源文件和输出目录。想调某个阶段，直接调那个节点函数（它们都是纯函数）。
- **检查点默认落 sqlite（`graph_state.sqlite`）**。一本书编译到一半被打断（断电、Ctrl-C、
  模型超时），用同一个 `thread_id` 再 invoke 一次就从断点续跑；没有它只能从头再来。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from langgraph.graph import END, StateGraph

from .nodes import (
    node_classify,
    node_containers,
    node_extract,
    node_gate,
    node_graph,
    node_index,
    node_llm_extract,
    node_llm_review,
    node_outline,
    node_profile,
    node_read,
    node_report,
    node_selfcheck,
    node_split,
    node_store,
    reset_caches,
    route_llm,
    route_selfcheck,
)
from .state import CompileState, initial_state

NODES = {
    "read": node_read,
    "profile": node_profile,
    "outline": node_outline,
    "gate": node_gate,
    "split": node_split,
    "selfcheck": node_selfcheck,
    "classify": node_classify,
    "llm_review": node_llm_review,
    "llm_extract": node_llm_extract,
    "store": node_store,
    "extract": node_extract,
    "graph": node_graph,
    "index": node_index,
    "containers": node_containers,
    "report": node_report,
}


def _route_after_read(state: CompileState) -> str:
    return "stop" if state.get("errors") else "profile"


def build_graph(checkpointer=None, *, interrupt_before: list[str] | None = None):
    """组装并编译 StateGraph。

    `interrupt_before` 让图在指定节点前停下并保存检查点 —— 这是"人工审核断点"的实现方式：
    例如 `interrupt_before=["store"]` 时，切分与归类做完就停，人可以看过 `blocks` 再放行，
    放行用 `resume_compile()`（或 `graph.invoke(None, cfg)`）。写库之前的那道人工闸门
    就靠它，而不是靠"记得先别跑最后一步"。
    """
    g = StateGraph(CompileState)
    for name, fn in NODES.items():
        g.add_node(name, fn)

    g.set_entry_point("read")
    g.add_conditional_edges("read", _route_after_read, {"stop": END, "profile": "profile"})
    g.add_edge("profile", "outline")
    g.add_edge("outline", "gate")
    g.add_edge("gate", "split")
    g.add_edge("split", "selfcheck")

    # 自检自环：还有缺陷且没到轮数上限 → 再修一轮。这是图上唯一的循环。
    g.add_conditional_edges("selfcheck", route_selfcheck, {"repair": "selfcheck", "classify": "classify"})

    # 归类之后是否叫模型：默认关闭时直接进 store，整条 LLM 分支不会被走到。
    g.add_conditional_edges("classify", route_llm, {"llm_review": "llm_review", "store": "store"})
    g.add_edge("llm_review", "store")

    g.add_edge("store", "extract")
    # 规则抽完之后，模型再补一遍"叙述里的实体"（规则抽版式、模型抽语义，写进同一张表）
    g.add_edge("extract", "llm_extract")
    g.add_edge("llm_extract", "graph")
    g.add_edge("graph", "index")
    g.add_edge("index", "containers")
    g.add_edge("containers", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer, interrupt_before=interrupt_before or ())


def make_checkpointer(out_dir: str | Path, prefer: str = "sqlite"):
    """检查点后端。sqlite 优先（能跨进程续跑），装不上就退到内存（只能同进程）。"""
    out = Path(out_dir)
    if prefer == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            out.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(out / "graph_state.sqlite"), check_same_thread=False)
            return SqliteSaver(con)
        except Exception:
            pass
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


def _config(thread_id: str, recursion_limit: int = 200) -> dict:
    # 200 足够：4 轮自检 + 12 个节点 = 16 步，留足余量给"未收敛时多修几轮"
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit}


def _drain(graph, payload, cfg, on_node):
    """跑一遍并（可选）逐节点回调。最终状态从检查点读，保证 trace 是累计的。"""
    if on_node is None:
        graph.invoke(payload, cfg)
    else:
        for chunk in graph.stream(payload, cfg, stream_mode="updates"):
            for name, upd in (chunk or {}).items():
                if isinstance(upd, dict):
                    on_node(name, upd)
    snap = graph.get_state(cfg)
    out = dict(snap.values or {})
    out["thread_id"] = cfg["configurable"]["thread_id"]
    return out


def run_compile(
    source_path: str,
    out_dir: str,
    *,
    book_id: str | None = None,
    limit_pages: int | None = None,
    with_vec: bool = True,
    options: dict | None = None,
    thread_id: str | None = None,
    checkpointer: str = "sqlite",
    on_node=None,
) -> dict:
    """一次性编译。返回最终状态（含 `report` 与 `verdict`）。"""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    state = initial_state(
        source_path, out_dir,
        book_id=book_id, limit_pages=limit_pages, with_vec=with_vec, options=options,
    )
    tid = thread_id or f"{book_id or Path(source_path).stem}:{int(time.time())}"
    graph = build_graph(make_checkpointer(out_dir, checkpointer))
    return _drain(graph, state, _config(tid), on_node)


def resume_compile(out_dir: str, thread_id: str, *, checkpointer: str = "sqlite", on_node=None) -> dict:
    """从检查点续跑。`thread_id` 用 `run_compile` 返回的那个。"""
    graph = build_graph(make_checkpointer(out_dir, checkpointer))
    return _drain(graph, None, _config(thread_id), on_node)


def graph_mermaid() -> str:
    """图的结构（Mermaid），贴文档/报告用。与 `build_graph` 的边保持一致。"""
    return "\n".join(
        [
            "graph TD",
            "  read[read 读入] -->|无错| profile[profile 版式画像]",
            "  read -->|源文件缺失| stop((END))",
            "  profile --> outline[outline 粗分章节]",
            "  outline --> gate[gate 非游戏内容闸门]",
            "  gate --> split[split 细分块]",
            "  split --> selfcheck{selfcheck 自检}",
            "  selfcheck -->|未收敛 & 未到上限| selfcheck",
            "  selfcheck -->|收敛| classify[classify 规范归类]",
            "  classify -->|启用 LLM 且有待判块| llm[llm_review 模型复核]",
            "  classify -->|默认离线| store[store 落盘]",
            "  llm --> store",
            "  store --> extract[extract 实体与字段]",
            "  extract --> graph2[graph 流程图/引用/场景]",
            "  graph2 --> index[index BM25+向量]",
            "  index --> report[report 报告与自证]",
            "  report --> done((END))",
        ]
    )


__all__ = [
    "NODES", "build_graph", "make_checkpointer", "run_compile", "resume_compile",
    "graph_mermaid", "reset_caches",
]
