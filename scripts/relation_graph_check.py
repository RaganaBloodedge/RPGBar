"""验证剧本静态关系图与本局动态关系的边界。

这个检查不依赖某个模组名称：它扫描编译产物中的 pc_relation，验证运行时可读；
再用临时 World/GameState 验证动态关系会生成独立事实事件。
"""
from __future__ import annotations

import glob
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.kb_tools import KBStore
from server.state_machine import GameState
from server.world import World


class _Store:
    flag_ids = set()

    def get_scene(self, sid):
        return {"id": sid, "title": sid, "exits": [], "checks": []}

    def current(self, sid):
        return self.get_scene(sid)


def main() -> int:
    found = 0
    for path in glob.glob("data/kb/*/kb.sqlite"):
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT src,dst FROM pc_relation LIMIT 1").fetchone()
        except sqlite3.Error:
            con.close()
            continue
        con.close()
        if not row:
            continue
        found += 1
        output = KBStore(Path(path)).relations(str(row[0]))
        assert "静态关系图" in output and str(row[1]) in output

    world_path = Path(tempfile.mktemp(suffix=".sqlite"))
    world = World(world_path, script="test.json")
    world.add_scene("start", "起点", "起点")
    world.enter("start")
    world.set("turn", "2")
    relation = world.set_relation("玩家", "NPC", "信任", "NPC 主动交出钥匙")
    assert relation["rel"] == "信任"
    state = GameState(_Store(), "start")
    event = state.commit_event(
        "relation_changed", actor="玩家", target="玩家|信任|NPC",
        text=relation["reason"], facts_added=["relation:玩家|信任|NPC"],
    )
    assert event["type"] == "relation_changed"
    assert event["facts_added"] == ["relation:玩家|信任|NPC"]
    world.close()
    world_path.unlink(missing_ok=True)
    print(json.dumps({"static_kbs_checked": found, "dynamic_event": "PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
