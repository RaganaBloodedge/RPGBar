"""服务端权威骰子，支持设置种子以便复现。"""
import random

_rng = random.Random()


def seed(n):
    _rng.seed(n)


def roll(d: int = 20) -> int:
    return _rng.randint(1, d)


def roll_check(skill: str, dc: int) -> dict:
    value = roll(20)
    return {"skill": skill, "dc": dc, "roll": value, "success": value >= dc}
