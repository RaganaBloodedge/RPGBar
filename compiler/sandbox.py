"""把一套规则书变成能直接开跑的**沙盒模组**。

为什么需要它：规则书不是剧本 —— 它有世界、规则、怪物、物品，但**没有场景图**
（没有「从哪里走到哪里、这里有什么可查」）。服务器的房间需要一份剧本才能开局，
所以规则书必须先"长"出一份沙盒：

```
KB（规则/物品/怪物/世界观）──生成器──► 沙盒剧本 JSON（6 个地点 + 检定）──► 房间
                                          └─ "kb": "<库名>"  ← DM 的工具靠它取库
```

生成的是**骨架**（地点、场景原文、内幕、检定、出口），不是小说：留给 DM 的
即兴空间比写死剧情更大，而且每个值都能回到规则书（技能名、DC 来自本书的技能表）。

骰子口径要说清楚：本服务器的检定是 **d20 + 属性加值 >= DC**，不是 COC 的 d100 掷低。
所以这里把难度折成 DC 10/13/16（简单/常规/困难），技能名照用本书的 —— 生成物能跑，
但要"原汁原味的 COC 手感"得给服务器加 d100 模式（见 docs 里的记录）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

PROMPT = """你是《克苏鲁的呼唤》第七版的守秘人。请用下面这本规则书里的素材，写一个
**1920 年代新英格兰**的沙盒模组（能反复来回走的小地图，不是线性剧情）。

【本书的素材】
主题：{topics}
怪物：{monsters}
物品：{items}
技能：{skills}

【要求】
- **6 个地点**，其中一个是起始地点；地点之间要能来回走；
- 每个地点：`title`（≤8 字）、`location`（一句话方位）、`scene`（2–3 句第二人称场景原文，
  必须有具体的感官细节：声音/气味/光影）、`secret`（DM 内幕：这里藏着什么、NPC 会说什么）、
  `keywords`（3–6 个）、`checks`（1–2 个）、`exits`（1–3 个）；
- 检定：`skill` **必须取自上面的技能名**；`dc` 只能取 10 / 13 / 16（简单/常规/困难）；
  `trigger` 给 2–4 个玩家会说的触发说法；`success` / `fail` 各一句具体的结果文案；
- 恐怖来自未知与代价，**不要写成打怪升级**；不要出现剧本之外的现代科技；
- 只输出 JSON，不要解释：

{{"title":"阿卡姆疑影","premise":"一句话背景","system":"给 DM 的语气与边界（2 句）",
 "start":"id",
 "scenes":[{{"id":"street","title":"市街","location":"阿卡姆·主街","scene":"……","secret":"……",
   "keywords":["街道","雾"],"checks":[{{"id":"spot","skill":"侦查","dc":13,
   "trigger":["打量","观察","找"],"success":"……","fail":"……"}}],
   "exits":[{{"to":"library","label":"去图书馆"}}]}}]}}"""

_WANT_SKILLS = ("侦查", "图书馆使用", "聆听", "心理学", "话术", "潜行", "闪避",
                "克苏鲁神话", "医学", "急救", "历史", "神秘学", "攀爬", "侦察")


def _kb_meta(kb_path: Path) -> dict:
    con = sqlite3.connect(f"file:{kb_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        topics = [r["label"] for r in con.execute(
            "SELECT label FROM lore_topic ORDER BY members DESC LIMIT 8")]
    except sqlite3.Error:
        topics = []
    try:
        monsters = [f"{r['name']}" for r in con.execute(
            "SELECT name FROM npc WHERE etype='monster' ORDER BY RANDOM() LIMIT 8")]
    except sqlite3.Error:
        monsters = []
    try:
        items = [r["name"] for r in con.execute(
            "SELECT name FROM item ORDER BY RANDOM() LIMIT 10")]
    except sqlite3.Error:
        items = []
    try:
        skills = [r["name"] for r in con.execute(
            "SELECT name FROM pc_skill WHERE name IN (%s) ORDER BY name"
            % ",".join("?" * len(_WANT_SKILLS)), _WANT_SKILLS)]
    except sqlite3.Error:
        skills = []
    if not skills:
        skills = list(_WANT_SKILLS)
    return {"topics": topics, "monsters": monsters, "items": items, "skills": skills}


def _ask(model, prompt: str) -> dict | None:
    from .segment import SegmentError, invoke

    try:
        return invoke(model, prompt, attempts=4, label="sandbox", max_tokens=2600)
    except SegmentError as exc:
        print(f"[sandbox] 模型没给出可用结果：{exc}")
        return None


def _fallback(meta: dict, kb_name: str) -> dict:
    """模型不可用时的最小沙盒：一个地点、几个检定。**不是剧情兜底**，是让房间能开局的骨架。"""
    skills = meta.get("skills") or list(_WANT_SKILLS)
    checks = []
    for k, sk in enumerate(skills[:4]):
        checks.append({
            "id": f"chk{k}", "skill": sk, "dc": 13,
            "keywords": [sk, "看看", "打量", "找"],
            "success": "你注意到了一处不对劲的细节，值得顺着查下去。",
            "fail": "你看不出更多东西，只有一阵说不清的不安。",
        })
    return {
        "title": f"{kb_name}·沙盒", "premise": "1920 年代的新英格兰，平静表面下有东西在动。",
        "system": "你是克苏鲁的呼唤的守秘人：描写具体的感官细节，让恐怖来自未知与代价。",
        "start": "main",
        "scenes": [{
            "id": "main", "title": "市街", "location": "阿卡姆·主街",
            "scene": "雾从港口漫上来，街灯在湿漉漉的石板上晕开。报摊的老板正收摊，"
                     "他看了你一眼，又迅速移开视线。",
            "secret": "报摊老板昨晚在码头看见有人往水里扔东西。",
            "keywords": ["街道", "雾", "报摊", "码头"],
            "checks": checks, "exits": [],
        }],
    }


def _to_script(raw: dict, meta: dict, kb_name: str) -> dict:
    """把模型给的骨架校验并转成服务器剧本格式。**不合格的条目直接丢**，不猜。"""
    skills = set(meta.get("skills") or [])
    scenes = []
    ids = set()
    for s in raw.get("scenes") or []:
        sid = re.sub(r"[^A-Za-z0-9_\-]", "", str(s.get("id") or ""))[:24]
        if not sid or sid in ids:
            continue
        ids.add(sid)
        scenes.append(s)
    if not scenes:
        raise ValueError("模型没给出任何地点")
    keep = {s["id"] for s in scenes}
    out_scenes = []
    for s in scenes:
        checks = []
        for k, c in enumerate(s.get("checks") or []):
            sk = str(c.get("skill") or "").strip()
            if skills and sk not in skills:
                continue
            dc = c.get("dc")
            if dc not in (10, 13, 16):
                dc = 13
            trig = [str(t) for t in (c.get("trigger") or []) if str(t).strip()]
            checks.append({
                "id": re.sub(r"[^A-Za-z0-9_\-]", "", str(c.get("id") or f"c{k}"))[:24] or f"c{k}",
                "skill": sk or (meta.get("skills") or ["侦查"])[0],
                "dc": dc,
                "keywords": trig[:6] or [sk, "看看"],
                "success": str(c.get("success") or "你查到了有用的东西。")[:80],
                "fail": str(c.get("fail") or "你没有发现更多。")[:80],
            })
        exits = []
        for e in s.get("exits") or []:
            to = str(e.get("to") or "").strip()
            if to in keep and to != s["id"]:
                exits.append({"to": to, "label": str(e.get("label") or to)[:20],
                              "keywords": [], "condition": None})
        out_scenes.append({
            "id": s["id"], "chapter": 1,
            "title": str(s.get("title") or s["id"])[:12],
            "location": str(s.get("location") or "")[:40],
            "public_text": str(s.get("scene") or "").strip()[:600],
            "dm_notes": str(s.get("secret") or "").strip()[:600],
            "npcs": [],
            "keywords": [str(k) for k in (s.get("keywords") or [])][:8],
            "flags_required": [], "flags_set": [],
            "checks": checks[:2], "exits": exits[:3],
        })
    start = str(raw.get("start") or "")
    if start not in {s["id"] for s in out_scenes}:
        start = out_scenes[0]["id"]
    return {
        "title": str(raw.get("title") or f"{kb_name}·沙盒")[:24],
        "premise": str(raw.get("premise") or "")[:200],
        "system": str(raw.get("system") or "")[:300],
        "start_scene": start,
        "players": {"min": 1, "max": 4, "recommended": 2},
        "flags": {},
        "kb": kb_name,
        "sandbox": True,
        "scenes": out_scenes,
    }


def build(kb_path: str | Path, out_path: str | Path, model=None) -> dict:
    kb_path = Path(kb_path)
    if kb_path.is_dir():
        kb_path = kb_path / "kb.sqlite"
    meta = _kb_meta(kb_path)
    kb_name = kb_path.parent.name
    raw = None
    if model is not None:
        prompt = (PROMPT.replace("{topics}", "、".join(meta["topics"]))
                  .replace("{monsters}", "、".join(meta["monsters"]))
                  .replace("{items}", "、".join(meta["items"]))
                  .replace("{skills}", "、".join(meta["skills"])))
        raw = _ask(model, prompt)
    if not raw or not raw.get("scenes"):
        print("[sandbox] 用最小骨架（模型不可用/输出不可用）")
        raw = _fallback(meta, kb_name)
    script = _to_script(raw, meta, kb_name)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "out": str(out_path), "scenes": len(script["scenes"]),
        "checks": sum(len(s["checks"]) for s in script["scenes"]),
        "exits": sum(len(s["exits"]) for s in script["scenes"]),
        "kb": kb_name,
    }
