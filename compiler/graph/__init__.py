"""剧本编译图（LangGraph）：把已验证的确定性内核装进标准 Agent 图。

为什么这么分层，说清楚免得后来人误解：

- `compiler/*.py`（reader/layout/outline/gate/spec/extract/...）是**内核**：基本是纯函数，
  不需要 LLM，同一个输入永远同一个输出。它们是这个 Agent 的"手脚"。
- `compiler/graph/*.py` 是**编排**：状态、节点、条件边、检查点。它决定"什么时候做什么、
  做不干净要不要回去重做"。这是这个 Agent 的"中枢"。
- `compiler/lc/*.py` 是**生态适配**：把内核包成 LangChain 的 Document / TextSplitter /
  Retriever / Tool，让外部 Agent 能直接调用，也让我们能复用 LangChain 的向量库与追踪。

一句话：**内核不依赖框架，框架只做编排与对外接口。** 换框架（或不用框架）都不必重写内核，
内核也能单测。
"""

from .build import (
    NODES,
    build_graph,
    graph_mermaid,
    make_checkpointer,
    reset_caches,
    resume_compile,
    run_compile,
)
from .state import CompileState, initial_state

__all__ = [
    "CompileState",
    "initial_state",
    "NODES",
    "build_graph",
    "make_checkpointer",
    "run_compile",
    "resume_compile",
    "graph_mermaid",
    "reset_caches",
]
