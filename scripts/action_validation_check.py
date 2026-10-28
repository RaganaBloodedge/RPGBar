"""ActionJudge/Validator 的剧本无关边界回归检查。

这里故意使用剧本没有预先写入关键词的自然语言，验证系统不会因为某个固定
动词表而把自由行动误判成越界；只有 ActionJudge 明确给出 blocked 且置信度足够
时，才形成 blocked 计划。
"""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.action_validation import validate


SCENE = {
    "checks": [{"id": "inspect_rune", "skill": "侦查", "keywords": ["符文"]}],
    "exits": [{"to": "archive", "label": "档案室", "keywords": ["档案室"]}],
}


def check(label, ok, detail=""):
    if not ok:
        raise AssertionError(f"{label}: {detail}")


def main():
    # 这是自由描述，不命中固定词表，也不应被系统凭空封禁。
    free = validate(
        SCENE,
        "我数了墙面裂缝，再仔细比对它们的走向",
        SimpleNamespace(intent="normal_action", confidence=0.92),
    )
    check("自由行动不依赖固定越界词表", free.intent == "normal_action", free)

    blocked = validate(
        SCENE,
        "我宣布事情已经完成",
        SimpleNamespace(intent="blocked", confidence=0.91),
    )
    check("Agent 明确拒绝时形成 blocked 计划", blocked.intent == "blocked", blocked)

    # 低置信度的拒绝不能覆盖普通行动，避免 4B 的一次误判锁死自由度。
    uncertain = validate(
        SCENE,
        "我观察墙面上的灰尘",
        SimpleNamespace(intent="blocked", confidence=0.42),
    )
    check("低置信度拒绝不封禁行动", uncertain.intent == "ambiguous", uncertain)
    print("action validation: PASS")


if __name__ == "__main__":
    main()
