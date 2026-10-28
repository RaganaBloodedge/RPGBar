"""ScriptKnowledgeAdapter 的静态/动态边界回归检查。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.rag import ScriptStore
from server.script_knowledge import ScriptKnowledgeAdapter


def main():
    store = ScriptStore(Path("scripts/sample_script.json"))
    adapter = ScriptKnowledgeAdapter(store)
    evidence = adapter.action_evidence("hall", flags={"found_secret_door"})
    secret = next(e for e in evidence["exits"] if e["to"] == "secret_room")
    assert secret["target_title"] == "密室"
    assert secret["available"] is True
    assert evidence["checks"] and evidence["npcs"] == []
    # 没有 World 时适配器不伪造动态实体/关系。
    assert "present_entities" not in evidence
    assert "dynamic_relations" not in evidence
    template = adapter.timeline_template()
    assert template["source"] == "kb_template"
    assert "events" in template and "entity_states" in template
    print("script knowledge adapter: PASS")


if __name__ == "__main__":
    main()
