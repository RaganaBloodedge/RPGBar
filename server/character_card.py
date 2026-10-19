"""角色卡：把「这个剧本需要哪些属性、每项多少点」变成一份**可校验的数据**。

为什么单独一层
--------------
剧本作者不一定愿意（也不一定会）手写一套属性表，所以**维度与点数由模型读一遍剧本来定**：
加载剧本 → 让 DM 槽位的模型看一份摘要 → 吐出一份 JSON 卡片 → 这里把它洗成合法数据。

跟「护栏做在工具里」是同一条原则：**模型给的是建议，落到游戏里的数据必须过校验**。
模型再怎么乱来（属性名超长、min > max、点数总量爆掉、skill_map 指向不存在的属性），
这里都能兜成一份能用的卡；实在洗不出来（返回空 dict）就当这一轮没产出 ——
按 2026-10-13 拍板的「没有模型就是没有产出」，不拿假数据顶替。

卡片的形状
----------
    {
      "required": true,                    # 这个本要不要玩家填角色卡
      "reason": "一句话说明为什么",
      "total_points": 6,                   # 人人相同的**可自由分配点数**
      "attributes": [                      # 2–8 项
        {"key": "力量", "desc": "搬、劈、扛",
         "min": 8, "base": 10, "max": 16}
      ],
      "skill_map": [{"skill": "战斗", "attr": "力量"}]   # 剧本里的检定技能 → 用哪个属性
    }

`skill_map` 让模型写成**列表**而不是对象：对象要求「键是技能名」，小模型很容易把键
全写成同一个词（实测 4B 吐过 `{"战斗":"力量","战斗":"聆听",…}`），而 JSON 的重复键
会被静默折叠成最后一条 —— 映射就悄悄错了。列表没有这个坑；两种写法这里都收。

点数怎么花：**base 是人人白送的基础值**，玩家把 `total_points` 点加在 base 之上，
每项最多加到 `max`，加的总量不超过 `total_points`。这样「总点数」只有一份，
界面上一排加减就够，服务端校验也简单（见 `validate_allocation`）。

检定怎么算：`d20 + 修正 >= DC`，`修正 = (属性值 - 10) // 2`（10 为基准，每 2 点 ±1）。
基准值特意定成 10，是为了让「没填卡的人」等于 +0 —— 少一个人不会把难度带偏。

⚠️ 公式只在这一处：`modifier()`。想换成「掷骰 <= 属性」那类玩法，改这一个函数即可。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .config import USER_DIR

# 分析结果按**剧本内容**缓存：同一份剧本只花一次模型调用，换机器也一样能命中（跟着 data/ 走）。
CARD_DIR = USER_DIR / "data" / "cards"

MAX_ATTRS = 8  # 属性再多，等待室里那一屏就塞不下了，而且没人会认真读
MAX_TOTAL_POINTS = 40  # 硬上限：防止模型给出「每人 200 点」这种把 DC 全废掉的数
ATTR_HARD_MIN = 1
ATTR_HARD_MAX = 30
MAX_KEY_LEN = 12
MAX_DESC_LEN = 40
MAX_REASON_LEN = 120

_DEFAULT_MIN = 8
_DEFAULT_BASE = 10
_DEFAULT_MAX = 16


# ---------------------------------------------------------------- 小工具


def _int(v):
    """宽容取整：字符串 "12" 也认；布尔与 None 不算数字（`True` 不该变成 1）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip():
        try:
            return int(float(v.strip()))
        except ValueError:
            return None
    return None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _text(v, limit):
    return str(v or "").strip()[:limit]


# ---------------------------------------------------------------- 检定加值


def modifier(value) -> int:
    """属性值 → 检定加值。10 为基准：每 2 点 ±1，向下取整。

    8 → -1、10 → 0、12 → +1、16 → +3。基准取 10 是刻意的 —— 没填卡的人是 +0，
    不会因为「少了一个人填卡」把整桌的难度带偏。
    """
    return (int(value) - 10) // 2


def attr_for_skill(card, skill: str):
    """剧本里的检定技能 → 用哪一项属性。找不到返回 None（= 不加值）。"""
    if not card or not card.get("required"):
        return None
    skill = str(skill or "").strip()
    if not skill:
        return None
    smap = card.get("skill_map") or {}
    if skill in smap:
        return smap[skill]
    # 模型偶尔会把技能名写长一点（"学识检定" vs "学识"）：双向包含也算命中
    for name, attr in smap.items():
        if name and (name in skill or skill in name):
            return attr
    # 最后一层：**技能名与属性同名**就用它。
    # 这一层是确定性的名字匹配（不是猜语义）——小模型很爱直接把剧本的检定技能
    # 当成属性名（力量/侦查/聆听…），这时哪怕 skill_map 漏了，也能对上一大半。
    for a in card.get("attributes") or []:
        if a.get("key") == skill:
            return skill
    return None


def modifier_for(card, character, skill: str = "") -> tuple:
    """按某位玩家（+某个检定技能）算出 (加值, 用到的属性名)。

    没有卡片、没填、没映射到属性 —— 一律 (0, "")，也就是「这一掷跟属性无关」。
    """
    if not card or not card.get("required"):
        return 0, ""
    attr = attr_for_skill(card, skill)
    if not attr:
        return 0, ""
    attrs = (character or {}).get("attrs") or {}
    val = _int(attrs.get(attr))
    if val is None:
        return 0, ""
    return modifier(val), attr


def summary(card, character) -> str:
    """「力量 14 · 学识 12」——给提示词与界面用的一行摘要（没填卡则返回空串）。"""
    if not card or not card.get("required"):
        return ""
    attrs = (character or {}).get("attrs") or {}
    if not attrs:
        return ""
    parts = []
    for a in card.get("attributes") or []:
        key = a.get("key")
        if key in attrs:
            parts.append(f"{key} {_int(attrs.get(key)) or 0}")
    return " · ".join(parts)


def player_line(name, character, card) -> str:
    """「亚瑟(战士 · 力量 14 · 学识 12)」——DM / 旁白提示词里的「在场玩家」一行。

    没卡时退回原来的「名字(职业)」，一个字都不多；连职业都没有就只留名字。
    """
    ch = character or {}
    bits = []
    if ch.get("cls"):
        bits.append(str(ch["cls"]))
    attrs = summary(card, ch)
    if attrs:
        bits.append(attrs)
    return f"{name}({' · '.join(bits)})" if bits else str(name)


# ---------------------------------------------------------------- 规范化


def normalize_card(raw) -> dict:
    """模型给的原始 JSON → 合法卡片。返回 `{}` 表示**不合格**（这一轮算没产出）。

    `{"required": false}` 是**合法的结论**（这个本不需要角色卡），会原样放行 ——
    「不需要」和「没分析出来」是两件事，界面要能分开显示。
    """
    if not isinstance(raw, dict) or not raw:
        return {}

    reason = _text(raw.get("reason"), MAX_REASON_LEN)

    # required 明确是 false ⇒ 不需要角色卡。没给这个字段但有属性表，按「需要」处理。
    if "required" in raw and not bool(raw.get("required")):
        if not reason:
            reason = "这个剧本不需要角色卡"
        return {"required": False, "reason": reason, "total_points": 0,
                "attributes": [], "skill_map": {}}

    attrs_in = raw.get("attributes")
    if not isinstance(attrs_in, list):
        return {}

    attrs, seen = [], set()
    for item in attrs_in:
        if not isinstance(item, dict):
            continue
        key = _text(item.get("key") or item.get("label") or item.get("name"), MAX_KEY_LEN)
        if not key or key in seen:
            continue
        lo = _int(item.get("min"))
        hi = _int(item.get("max"))
        base = _int(item.get("base"))
        lo = _DEFAULT_MIN if lo is None else _clamp(lo, ATTR_HARD_MIN, ATTR_HARD_MAX - 1)
        hi = _DEFAULT_MAX if hi is None else _clamp(hi, ATTR_HARD_MIN + 1, ATTR_HARD_MAX)
        if lo >= hi:
            # 区间反了 / 一样大：按缺省重来，而不是把这一项丢掉（丢掉会让 skill_map 对不上）
            lo, hi = _DEFAULT_MIN, _DEFAULT_MAX
        base = lo if base is None else _clamp(base, lo, hi)
        seen.add(key)
        attrs.append({
            "key": key,
            "desc": _text(item.get("desc"), MAX_DESC_LEN),
            "min": lo, "base": base, "max": hi,
        })
        if len(attrs) >= MAX_ATTRS:
            break

    if not attrs:
        return {}

    total = _int(raw.get("total_points"))
    total = 0 if total is None else _clamp(total, 0, MAX_TOTAL_POINTS)

    # skill_map：只保留「值确实是已有属性」的那些
    # 两种写法都收：
    #   [{"skill": "战斗", "attr": "力量"}]   ← 给模型用的形状（**列表**）
    #   {"战斗": "力量"}                     ← 手写剧本/老缓存里的形状
    # 为什么让模型写列表：对象要求「键是技能名」，小模型很容易把键写成同一个词
    # （实测 4B 吐出 `{"战斗":"力量","战斗":"聆听",…}`），而 JSON 对象重复键会被
    # 静默折叠成最后一条 —— 映射就悄悄错了。列表没有这个坑。
    smap = {}
    keys = {a["key"] for a in attrs}
    raw_map = raw.get("skill_map")
    pairs = []
    if isinstance(raw_map, list):
        for item in raw_map:
            if isinstance(item, dict):
                pairs.append((item.get("skill") or item.get("key"), item.get("attr") or item.get("value")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
    elif isinstance(raw_map, dict):
        pairs = list(raw_map.items())
    for k, v in pairs:
        k = _text(k, MAX_KEY_LEN + 4)
        v = _text(v, MAX_KEY_LEN)
        if k and v in keys:
            smap[k] = v

    return {
        "required": True,
        "reason": reason,
        "total_points": total,
        "attributes": attrs,
        "skill_map": smap,
    }


# ---------------------------------------------------------------- 点数分配校验


def spent(card, values) -> int:
    """已经花掉多少点（只算 base 之上的部分；没给的项按 base 算）。"""
    total = 0
    for a in (card or {}).get("attributes") or []:
        v = _int((values or {}).get(a["key"]))
        if v is None:
            v = a["base"]
        total += max(0, v - a["base"])
    return total


def remaining(card, values) -> int:
    """还剩多少点可分配（可能为 0，不会是负的 —— 超额的提交会被拒）。"""
    return max(0, int((card or {}).get("total_points") or 0) - spent(card, values))


def validate_allocation(card, values) -> tuple:
    """校验一次提交 → (清洗后的属性表, 错误列表)。

    规则（服务端权威，前端只做提示）：
    - 每一项都得是数字，且落在 `min`–`max`；
    - 不能低于 `base`（base 是白送的，扣不回来 —— 免得玩家「自残」去换点数）；
    - 一共花掉的点数不超过 `total_points`。**允许留空**（不强制花满）。
    """
    if not card or not card.get("required"):
        return {}, ["这个剧本不需要角色卡"]
    if not isinstance(values, dict):
        return {}, ["角色卡格式不对"]

    errors, clean = [], {}
    for a in card.get("attributes") or []:
        key = a["key"]
        v = _int(values.get(key, a["base"]))
        if v is None:
            errors.append(f"「{key}」要填一个数字")
            continue
        if v < a["min"]:
            errors.append(f"「{key}」不能低于 {a['min']}")
            continue
        if v > a["max"]:
            errors.append(f"「{key}」不能高于 {a['max']}")
            continue
        if v < a["base"]:
            errors.append(f"「{key}」不能低于基础值 {a['base']}")
            continue
        clean[key] = v

    if errors:
        return {}, errors

    used = spent(card, clean)
    if used > int(card.get("total_points") or 0):
        return {}, [f"点数超了：一共 {card['total_points']} 点，你用了 {used} 点"]

    return clean, []


def to_client(card) -> dict:
    """下发给前端的卡片（**不含 skill_map**）。

    skill_map 是 DM 侧的机制映射（哪个检定吃哪个属性）。把它发给玩家等于把
    「这个本有哪些检定」摊开 —— 属于剧本内幕，和 dm_notes 一个待遇。
    """
    if not card:
        return {}
    return {
        "required": bool(card.get("required")),
        "reason": card.get("reason", ""),
        "total_points": int(card.get("total_points") or 0),
        "attributes": [
            {"key": a["key"], "desc": a.get("desc", ""),
             "min": a["min"], "base": a["base"], "max": a["max"]}
            for a in card.get("attributes") or []
        ],
    }


# ---------------------------------------------------------------- 缓存


def card_key(store) -> str:
    """剧本**内容**指纹。同一份剧本（哪怕文件名不同）只分析一次。"""
    data = getattr(store, "data", None)
    if not isinstance(data, dict):
        data = {}
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    blob += "\n" + str(getattr(store, "title", "")) + "\n" + str(getattr(store, "system", ""))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return CARD_DIR / f"{key}.json"


def cache_path(key: str) -> Path:
    """缓存文件路径（公开版）。

    给测试与排障用：能先把旧文件备份下来、用完还原 —— 否则用例往真实缓存放一张假卡，
    这台机器上所有房间都会一直用那张假卡（缓存是按剧本内容命中的）。
    """
    return _cache_path(key)


def load_cached(key: str):
    """读缓存。损坏/不存在一律返回 None（重新分析一次，不抛异常）。"""
    p = _cache_path(key)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    card = obj.get("card")
    # 缓存也要过一遍规范化：手改过的缓存文件不该把坏数据带进游戏
    return normalize_card(card) if isinstance(card, dict) else None


def save_cached(key: str, card: dict, title: str = "") -> None:
    """落盘（先写临时文件再原子替换，避免半截 JSON）。写不进去不算错。"""
    if not card:
        return
    try:
        CARD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(key).with_suffix(".tmp")
        payload = {"key": key, "title": title, "card": card}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _cache_path(key))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 给模型的剧本摘要


def script_digest(store, limit: int = 6000) -> str:
    """把剧本压成一份「够模型判断、又不会撑爆上下文」的摘要。

    刻意**不带 dm_notes**：设计角色卡不需要主持人的底牌，少一分泄露的风险。
    """
    lines = [f"【剧本】《{getattr(store, 'title', '')}》"]
    premise = getattr(store, "premise", "") or ""
    system = getattr(store, "system", "") or ""
    if premise:
        lines.append(f"【背景】{premise[:400]}")
    if system:
        lines.append(f"【基调】{system[:600]}")

    scenes = list((getattr(store, "scenes", {}) or {}).values())
    lines.append("")
    lines.append(f"【场景】共 {len(scenes)} 个")
    for i, s in enumerate(scenes):
        title = s.get("title") or s.get("id") or ""
        body = (s.get("public_text") or "").strip().replace("\n", " ")
        lines.append(f"{i + 1}. {title}：{body[:220]}")
        checks = s.get("checks") or []
        if checks:
            labels = "、".join(f"{c.get('skill', '通用')}（DC {c.get('dc', 10)}）" for c in checks)
            lines.append(f"   · 检定：{labels}")
        npcs = [str(n.get("name") or "") for n in (s.get("npcs") or []) if n.get("name")]
        if npcs:
            lines.append(f"   · 出场：{'、'.join(npcs)}")

    skills = sorted({str(c.get("skill") or "") for s in scenes for c in (s.get("checks") or [])} - {""})
    lines.append("")
    if skills:
        lines.append("【剧本里出现过的检定技能】" + "、".join(skills))
        lines.append("skill_map 必须把这些技能**全部**映射到某个属性。")
    else:
        lines.append("【剧本里出现过的检定技能】无（这个本没有任何检定）")

    text = "\n".join(lines)
    if len(text) > limit:
        # 超长就砍中间的场景：头尾都在，模型仍能看懂这是个什么本
        head = text[: limit // 2]
        tail = text[-limit // 2 :]
        text = head + "\n…（中间省略若干场景）…\n" + tail
    return text
