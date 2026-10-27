"""防止不同剧本错误共享规则库或 CoC 判定档案。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.kb_tools import kb_for_script
from server.rules import Rules


def main() -> int:
    coc_kb = kb_for_script("coc_sandbox.json")
    tomb_kb = kb_for_script("totsk_l1.json")
    assert coc_kb is not None, "CoC 沙盒没有绑定规则知识库"
    assert Rules.load(coc_kb).is_d100, "CoC 沙盒没有使用 d100 规则档案"
    assert tomb_kb is None, "蛇王墓意外绑定了知识库"
    assert not Rules.load(tomb_kb).is_d100, "蛇王墓意外使用了 CoC d100"
    print("rules binding: PASS (CoC=d100, Tomb=d20 fallback, KB isolated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
