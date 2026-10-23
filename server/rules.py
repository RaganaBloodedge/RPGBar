"""判定引擎：按**规则书的档案**造骰子，而不是写死 d20。

档案来自编译期（`compiler/rules_profile.py` → `data/kb/<库名>/rule_profile.json`）：

    COC: {"check":{"dice":"d100","roll":"under","target":"skill_value"},
          "levels":{"regular":1.0,"hard":0.5,"extreme":0.2}, "sanity":{...}}
    其他: {"check":{"dice":"d20","roll":"over","target":"dc"}}

分工：**模型永远拿不到骰子**。它只能说"要做一次侦查检定、难度困难"，
值、掷、成功等级都由这里算；没有档案时退回旧的 d20 逻辑（兼容老剧本）。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

_rng = random.Random()

D20_PROFILE = {
    "check": {"dice": "d20", "roll": "over", "target": "dc"},
    "levels": None,
    "sanity": None,
}


def seed(n: int) -> None:
    _rng.seed(n)


class Rules:
    """一份规则档案 + 掷骰。`d100` 是掷低（COC），`d20` 是加值比高。"""

    def __init__(self, profile: dict | None = None):
        p = profile or {}
        chk = p.get("check") or {}
        self.dice = str(chk.get("dice") or "d20").lower()
        self.roll = str(chk.get("roll") or ("under" if self.dice == "d100" else "over")).lower()
        self.target = str(chk.get("target") or ("skill_value" if self.dice == "d100" else "dc"))
        self.levels = p.get("levels") or None
        self.sanity = p.get("sanity") or None
        self.bonus_penalty = p.get("bonus_penalty")
        self.raw = p

    @classmethod
    def load(cls, kb_path: str | Path | None) -> "Rules":
        """KB 目录旁的 rule_profile.json；没有就是 None（调用方退回 d20）。"""
        if not kb_path:
            return cls(None)
        p = Path(kb_path)
        cand = p.parent / "rule_profile.json" if p.name == "kb.sqlite" else p / "rule_profile.json"
        if not cand.exists():
            return cls(None)
        try:
            return cls(json.loads(cand.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001  档案坏了不该让游戏起不来
            return cls(None)

    @property
    def is_d100(self) -> bool:
        return self.dice == "d100"

    def level_factor(self, difficulty: str = "regular") -> float:
        d = (difficulty or "regular").strip().lower()
        lv = self.levels or {}
        if d in ("hard", "困难"):
            return float(lv.get("hard") or 0.5)
        if d in ("extreme", "极限"):
            return float(lv.get("extreme") or 0.2)
        return float(lv.get("regular") or 1.0)

    def check(self, skill: str, skill_value: int | None, dc: int = 10,
              difficulty: str = "regular", mod: int = 0) -> dict:
        """做一次检定。返回的结果**就是权威**，模型只负责念出来。"""
        if self.is_d100:
            base = int(skill_value or 0) or 30
            target = max(1, int(base * self.level_factor(difficulty)))
            roll = _rng.randint(1, 100)
            success = roll <= target
            # COC 的成功等级：极限 ≤ 1/5，困难 ≤ 1/2，常规 ≤ 技能值
            if roll <= max(1, base // 5):
                level = "extreme"
            elif roll <= max(1, base // 2):
                level = "hard"
            elif roll <= base:
                level = "regular"
            else:
                level = "fail"
            if roll == 100 or (roll >= 96 and base < 50):
                level = "fumble"
            if roll == 1:
                level = "critical"
            return {"dice": "d100", "roll": roll, "target": target, "skill_value": base,
                    "difficulty": difficulty or "regular", "level": level,
                    "success": success, "skill": skill}
        value = _rng.randint(1, 20)
        total = value + int(mod or 0)
        return {"dice": "d20", "roll": value, "target": int(dc or 10), "mod": int(mod or 0),
                "total": total, "level": "regular" if total >= int(dc or 10) else "fail",
                "success": total >= int(dc or 10), "skill": skill}

    # ---------------------------------------------------------------- 理智
    def sanity_check(self, current: int) -> dict:
        """COC 的理智检定：d100 掷低于当前 SAN。"""
        if not self.is_d100:
            return self.check("理智", current, dc=10)
        roll = _rng.randint(1, 100)
        return {"dice": "d100", "roll": roll, "target": int(current), "success": roll <= int(current),
                "level": "regular" if roll <= int(current) else "fail", "skill": "理智"}

    def describe(self) -> str:
        if self.is_d100:
            return f"d100 掷低（目标=技能值；困难×{self.level_factor('hard'):g}，" \
                   f"极限×{self.level_factor('extreme'):g}）"
        return "d20 + 加值 vs DC"
