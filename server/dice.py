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


def weighted_pick(weights: list) -> tuple:
    """按权重表采样：返回 (选中下标, 采样值, 权重总和)。

    与 `roll_check` 同一条铁律——**骰子只有一个来源**：模型只给权重（区间），
    真正落在哪个区间由这里定，模型拿不到、也编不出结果。
    权重全为 0（或空）时退化为等概率；空表返回 (-1, 0, 0)。
    """
    n = len(weights)
    if n == 0:
        return -1, 0.0, 0.0
    clean = [max(0.0, float(w or 0)) for w in weights]
    total = sum(clean)
    if total <= 0:
        idx = _rng.randrange(n)
        return idx, 1.0 * idx / n, 0.0
    r = _rng.random() * total
    acc = 0.0
    for i, w in enumerate(clean):
        acc += w
        if r < acc:
            return i, r, total
    return n - 1, r, total
