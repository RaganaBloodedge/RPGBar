"""Small acceptance check for Chronicle's temporal retrieval contract."""
from __future__ import annotations

import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.chronicle import Chronicle


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        c = Chronicle(Path(d) / "chronicle.jsonl")
        c.add("调查大厅的挂毯", turn=1, scene="大厅", kind="action", actors=["调查员"])
        c.add("调查员取得黄铜钥匙", turn=2, scene="大厅", kind="fact", actors=["调查员"])
        c.add("调查员进入地下密室", turn=3, scene="密室", kind="scene", actors=["调查员"])

        exact = c.search("黄铜钥匙", after_turn=2, before_turn=2)
        assert len(exact) == 1 and exact[0]["turn"] == 2

        scene = c.search("调查员", scene="大厅")
        assert scene and all(row["scene"] == "大厅" for row in scene)

        kinds = c.search("调查员", kinds=["scene"])
        assert kinds and all(row["kind"] == "scene" for row in kinds)

        actor_time = c.search("调查员", actor="调查员", before_turn=2)
        assert actor_time and all(row["turn"] <= 2 for row in actor_time)
        assert c.search("调查员", actor="不存在的角色") == []

        reloaded = Chronicle(Path(d) / "chronicle.jsonl")
        assert reloaded.search("黄铜钥匙", after_turn=2, before_turn=2)[0]["turn"] == 2
    print("temporal chronicle checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
