"""图状态（LangGraph 的 State）。

三条硬规矩，决定了这个图能不能真的跑长任务：

1. **状态里只放 JSON 可序列化的东西。** Book / Line / Table 这些带 bbox 的 dataclass
   一律不进状态 —— 一本 418 页的书有十几万个 span，塞进状态会让检查点膨胀到几十 MB。
   它们按 `source_path` 从磁盘重建（见 `nodes._book()` 的缓存），状态里只留摘要。
2. **块只存扁平字段。** `lines`（每行的坐标）在切分那一刻用完就丢；块之间的父子关系靠
   `id` 与 `meta` 表达。
3. **能覆盖就覆盖。** 只有 `trace` / `errors` / `warnings` 用 reducer，其余字段是
   "节点返回什么就是什么" —— 重切时块必须整体替换，不能累加。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class CompileState(TypedDict, total=False):
    # ---------------- 输入（一次给定，之后只读）
    source_path: str
    out_dir: str
    book_id: str
    limit_pages: int | None
    with_vec: bool
    options: dict[str, Any]

    # ---------------- 各阶段产物
    book_meta: dict
    layout: dict
    sections: list[dict]
    page_verdicts: dict[str, dict]
    denied: dict
    blocks: list[dict]
    block_tables: dict[str, list[dict]]
    """block_id -> 表格转储。扁平化会丢 Table 对象，抽取阶段要用它重建。"""
    llm_split: dict
    """LLM 切分统计：{sections, windows, calls, repairs, open_carry, entries, issues}。"""
    selfcheck: dict
    classes: dict[str, dict]
    """block_id -> {kind, subtype, confidence, evidence, canonical, by}；by=llm。"""
    disputed: list[dict]
    """争议池：LLM 有意见但被护栏拒掉的块。落盘 disputed.json 供人工裁决。"""
    store: dict
    extract: dict
    llm: dict
    """LLM 判别的调用统计：{calls, ok, rejected, items, failed_calls}。"""
    index: dict
    graph: dict
    containers: dict
    """五类容器的产出统计：{rule, pc, tool, npc, lore}"""
    report: dict

    # ---------------- 控制
    stage: str
    round: int
    trace: Annotated[list[dict], operator.add]
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]

    # ---------------- 收尾结论
    verdict: dict
    """{'passed': bool, 'reasons': [...], 'metrics': {...}}"""


DEFAULTS: dict[str, Any] = {
    "limit_pages": None,
    "with_vec": True,
    "options": {
        "max_chars": 520,
        "max_rounds": 4,
        "min_compliance": 0.99,
        "max_residual": 0.005,
        "use_llm": False,
        # 判定权只归模型：use_llm=true 时默认走 LLM 切分；
        # 关掉它 = 显式退回版式启发（只该在离线测试时用）。
        "llm_split": True,
        "llm_split_chars": 2000,
        "llm_min_conf": 0.5,
        "llm_budget": 200,
    },
    "round": 0,
    "trace": [],
    "errors": [],
    "warnings": [],
    "stage": "init",
}


def initial_state(
    source_path: str,
    out_dir: str,
    *,
    book_id: str | None = None,
    limit_pages: int | None = None,
    with_vec: bool = True,
    options: dict | None = None,
) -> CompileState:
    """构造初始状态。**不做任何 IO** —— 第一条边读文件，这样"输入校验失败"也是一条 trace。"""
    return {
        "source_path": str(source_path),
        "out_dir": str(out_dir),
        "book_id": book_id or "",
        "limit_pages": limit_pages,
        "with_vec": with_vec,
        "options": {**DEFAULTS["options"], **(options or {})},
        "round": 0,
        "trace": [],
        "errors": [],
        "warnings": [],
        "stage": "init",
    }


def is_llm_enabled(state: CompileState) -> bool:
    return bool((state.get("options") or {}).get("use_llm"))


def opt(state: CompileState, key: str, default=None):
    return (state.get("options") or {}).get(key, DEFAULTS["options"].get(key, default))
