"""从规则书抽「判定档案」：服务端据此造骰子，而不是写死 d20。

一份档案只回答"这本书怎么判定"：

    {"check": {"dice": "d100", "roll": "under", "target": "skill_value"},
     "levels": {"regular": 1.0, "hard": 0.5, "extreme": 0.2},
     "opposed": "compare_levels", "bonus_penalty": "tens_digit",
     "sanity": {"dice": "d100", "under": "SAN"}}

抽法：先问模型（带引文校验），抽不到再退回**确定性检测**（书里明写"百分骰（D100）"、
"属性百分骰除以２…一半" 这类句子本身就是证据）。两条路都留下 `source`，
DM 侧能看到这份档案是"模型判的"还是"文本检测的"。

存哪：`data/kb/<库名>/rule_profile.json`，和 kb.sqlite 同目录 —— 只读产物的一部分。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

PROMPT = """下面是一本跑团规则书里关于「判定怎么掷」的原文片段。请判断它用的是什么判定方式。

{blocks}

只输出 JSON，字段如下（不确定就填 null）：
{{"system": "书的名字或规则系统名，如 coc7e",
 "check": {{"dice": "d100 或 d20", "roll": "under 或 over", "target": "skill_value 或 attribute 或 dc"}},
 "levels": {{"regular": 1.0, "hard": 0.5, "extreme": 0.2}},
 "opposed": "compare_levels 或 compare_total 或 null",
 "bonus_penalty": "tens_digit 或 advantage 或 null",
 "sanity": {{"dice": "d100 或 null", "under": "SAN 或 null"}},
 "evidence": ["逐字摘自原文的判断依据，2-4 条"]}}

注意：
- "d100 / 百分骰 / 掷低于技能值" → dice=d100, roll=under, target=skill_value；
- "d20 + 加值 >= DC" → dice=d20, roll=over, target=dc；
- levels 是难度把目标值打折的系数（一半 = 0.5，五分之一 = 0.2），没有就给 null。"""


def _blocks(con: sqlite3.Connection, limit: int = 6) -> list[tuple[str, str]]:
    rows = con.execute(
        "SELECT title,text FROM block WHERE kind='rule' AND COALESCE(canonical,1)=1 AND ("
        "text LIKE '%d100%' OR text LIKE '%D100%' OR text LIKE '%百分骰%' "
        "OR text LIKE '%小于等于%' OR text LIKE '%困难难度%' OR text LIKE '%奖励骰%' "
        "OR text LIKE '%一半%' OR text LIKE '%d20%') LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _detect(text: str) -> dict:
    """确定性兜底：只认书里明写的证据，不做语义猜测。"""
    t = text or ""
    d100 = bool(re.search(r"(百分骰|d100|D100)", t))
    under = bool(re.search(r"(小于等于|不超过|掷出.*低于|不高于)", t))
    half = bool(re.search(r"(除以２|除以2|一半)", t))
    fifth = bool(re.search(r"(五分之一)", t))
    profile = {
        "system": "",
        "check": {
            "dice": "d100" if d100 else "d20",
            # d100 体系都是"掷低"（COC 系列）；d20 体系才是"加值比高"。
            # under 只当证据，不当开关 —— 否则漏一句话就把方向判反。
            "roll": "under" if d100 else "over",
            "target": "skill_value" if d100 else "dc",
        },
        "levels": ({"regular": 1.0, "hard": 0.5, "extreme": 0.2}
                   if (half or fifth) else None),
        "opposed": None,
        "bonus_penalty": "tens_digit" if re.search(r"奖励骰|惩罚骰", t) else None,
        "sanity": {"dice": "d100", "under": "SAN"} if re.search(r"理智|SAN", t) else None,
        "evidence": [],
        "source": "detect",
    }
    for pat in (r".{0,20}(百分骰|D100|d100).{0,30}", r".{0,10}除以[２2].{0,20}",
                r".{0,6}五分之一.{0,16}"):
        m = re.search(pat, t)
        if m:
            profile["evidence"].append(re.sub(r"\s+", "", m.group(0))[:60])
    return profile


def _verify(raw: dict, text: str) -> dict:
    """模型给的档案要过基本校验：骰子/方向/目标值必须是合法值，证据要能在原文找到。"""
    out = {
        "system": str(raw.get("system") or "")[:40],
        "check": {},
        "levels": raw.get("levels"),
        "opposed": raw.get("opposed"),
        "bonus_penalty": raw.get("bonus_penalty"),
        "sanity": raw.get("sanity"),
        "evidence": [],
        "source": "llm",
    }
    chk = raw.get("check") or {}
    dice = str(chk.get("dice") or "").lower()
    if dice not in ("d100", "d20"):
        dice = "d100" if "d100" in text.lower() else "d20"
    roll = str(chk.get("roll") or "").lower()
    if roll not in ("under", "over"):
        roll = "under" if dice == "d100" else "over"
    target = str(chk.get("target") or "").lower()
    if target not in ("skill_value", "attribute", "dc"):
        target = "skill_value" if dice == "d100" else "dc"
    out["check"] = {"dice": dice, "roll": roll, "target": target}
    flat = re.sub(r"\s+", "", text)
    for q in (raw.get("evidence") or [])[:6]:
        q2 = re.sub(r"\s+", "", str(q))
        if len(q2) >= 4 and q2 in flat:
            out["evidence"].append(q2[:80])
    if not out["evidence"]:
        out["source"] = "llm-unverified"
    lv = out.get("levels")
    if isinstance(lv, dict):
        clean = {}
        for k in ("regular", "hard", "extreme"):
            try:
                v = float(lv.get(k))
                if 0 < v <= 1:
                    clean[k] = v
            except (TypeError, ValueError):
                pass
        out["levels"] = clean or None
    return out


def build(kb_dir: str | Path, model=None) -> dict:
    kb_dir = Path(kb_dir)
    db = kb_dir / "kb.sqlite" if kb_dir.is_dir() else kb_dir
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        blocks = _blocks(con)
    finally:
        con.close()
    joined = "\n".join(t for _title, t in blocks)
    profile = None
    if model is not None and blocks:
        from .segment import SegmentError, invoke

        listing = "\n\n".join(f"《{t}》\n{x[:500]}" for t, x in blocks[:5])
        try:
            raw = invoke(model, PROMPT.replace("{blocks}", listing), attempts=3,
                         label="ruleprofile", max_tokens=700)
            if raw:
                profile = _verify(raw, joined)
        except SegmentError as exc:
            print(f"[ruleprofile] 模型没给出结果：{exc}")
    if not profile:
        profile = _detect(joined)
    out = kb_dir / "rule_profile.json"
    out.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"out": str(out), **profile}
