"""Session zero：点「开始游戏」那一刻，DM 先读库、定调、铺开场。

为什么要这一步：规则书是"世界 + 规则"，没有场景图。开局前 DM 得先知道
——这是哪个年代、什么调子、哪些怪物允许出现、从哪儿开始。
这些**不是每轮现编**，而是一次性决策，落进世界库，之后每轮当常量注入。

产物（写进世界层，可审查）：

    meta.bible   故事圣经：年代/语气/前提/可用怪物/禁忌
    scene(seed)  开场场景：地区 + 场景原文 + 内幕 + 3 个可动的钩子

规则书里有示例模组时优先用书里的（本书的"模组"章节），没有才生成。
"""
from __future__ import annotations

import json
import random
import re

PROMPT = """你是《{title}》的守秘人。请用这本规则书的素材，design 一个**沙盒开局**。

【本书素材】
世界观主题：{topics}
怪物（只能用这些）：{monsters}
技能：{skills}
判定：{rules}
物品样例：{items}

【要求】
1. 定一个**具体的年代与地点**，但不要默认 1925 年或阿卡姆；根据规则书素材和本局标题选择合适的时代与地点；
2. 写 2–4 句场景原文：第二人称、有感官细节（声音/气味/光），留出可探索的空间；
3. 给 DM 一句内幕：这里真正在发生什么；
4. 给 3 个**可动手的钩子**（一句话一个：一扇门、一个眼神、地上的拖痕……）；
5. `monsters` 只能从上面清单里挑，挑 2–3 个作为本局可能出现的威胁；
6. 只输出 JSON，不要解释：

 {{"era":"由本局素材决定的具体时代","place":"由本局素材决定的具体地点",
 "tone":"阴郁、克制，恐怖来自未知与代价",
 "premise":"一句话说清这局的核心冲突",
 "monsters":["深潜者"],
 "forbidden":["现代科技","超能力"],
 "scene":"2–4 句场景原文",
 "secret":"DM 内幕",
 "hooks":["钩子1","钩子2","钩子3"]}}"""


def _kb_brief(kb) -> dict:
    """从 KB 取开局的素材（都来自编译产物，不是编的）。"""
    if kb is None or not getattr(kb, "available", False):
        return {"topics": [], "monsters": [], "skills": [], "items": [], "rules": "未知"}
    con = kb.kb.con
    try:
        topics = [r[0] for r in con.execute(
            "SELECT label FROM lore_topic ORDER BY members DESC LIMIT 8")]
    except Exception:  # noqa: BLE001
        topics = []
    try:
        monsters = [r[0] for r in con.execute(
            "SELECT name FROM npc WHERE etype='monster' ORDER BY RANDOM() LIMIT 12")]
    except Exception:  # noqa: BLE001
        monsters = []
    try:
        skills = [r[0] for r in con.execute(
            "SELECT name FROM pc_skill ORDER BY RANDOM() LIMIT 12")]
    except Exception:  # noqa: BLE001
        skills = []
    try:
        items = [r[0] for r in con.execute("SELECT name FROM item ORDER BY RANDOM() LIMIT 8")]
    except Exception:  # noqa: BLE001
        items = []
    rules = "d100 掷低" if getattr(kb, "rules_d100", False) else "d20 vs DC"
    return {"topics": topics, "monsters": monsters, "skills": skills, "items": items,
            "rules": rules}


def _fallback(meta: dict, title: str) -> dict:
    """没有模型时的**确定性**圣经：不编剧情，只把书里的东西摆好。"""
    monsters = meta.get("monsters") or []
    topics = meta.get("topics") or []
    return {
        "era": "规则书未指定时代",
        "place": topics[0] if topics else "一处待探索的地点",
        "tone": "阴郁、克制；恐怖来自未知与代价",
        "premise": "一件看似平常的委托背后，有不该被打扰的东西。",
        "monsters": monsters[:2],
        "forbidden": ["现代科技", "超自然力量公开化"],
        "scene": "夜雨把街道冲得发亮，路灯在积水里碎成一片橘黄。"
                 "你站在一扇半掩的门前，门缝里有旧纸和铁锈的味道。",
        "secret": "屋里的人已经三天没出门了，而他昨夜还在码头出现过。",
        "hooks": ["门缝里透出的光", "地上湿漉漉的脚印", "屋里断掉的对话声"],
        "source": "fallback",
    }


def _verify(raw: dict, meta: dict) -> dict:
    monsters = set(meta.get("monsters") or [])
    picked = [m for m in (raw.get("monsters") or []) if str(m) in monsters][:3]
    if not picked:
        picked = list(monsters)[:2]
    hooks = [str(h)[:60] for h in (raw.get("hooks") or []) if str(h).strip()][:3]
    return {
        "era": str(raw.get("era") or "规则书未指定时代")[:30],
        "place": str(raw.get("place") or "")[:40],
        "tone": str(raw.get("tone") or "")[:60],
        "premise": str(raw.get("premise") or "")[:120],
        "monsters": picked,
        "forbidden": [str(x)[:20] for x in (raw.get("forbidden") or [])][:6],
        "scene": str(raw.get("scene") or "").strip()[:600],
        "secret": str(raw.get("secret") or "").strip()[:300],
        "hooks": hooks,
        "source": "llm",
    }


async def prepare(store, kb, world, provider=None, play=None) -> dict:
    """跑一次 session zero，结果落进世界层并返回（可广播、可审查）。"""
    meta = _kb_brief(kb)
    if play is not None:
        meta["play_mode"] = getattr(play, "mode_label", "")
        meta["play_rules"] = getattr(getattr(play, "rules", None), "profile", "")
    bible = None
    if provider is not None:
        prompt = (PROMPT.replace("{title}", store.title or "这本规则书")
                  .replace("{topics}", "、".join(meta["topics"]) or "（无）")
                  .replace("{monsters}", "、".join(meta["monsters"]) or "（无）")
                  .replace("{skills}", "、".join(meta["skills"]) or "（无）")
                  .replace("{items}", "、".join(meta["items"]) or "（无）")
                  .replace("{rules}", meta.get("play_rules") or meta["rules"])
        try:
            text = await provider.chat([{"role": "user", "content": prompt}], json_mode=True)
            raw = json.loads(text if isinstance(text, str) else str(text))
            if raw and raw.get("scene"):
                bible = _verify(raw, meta)
        except Exception as exc:  # noqa: BLE001  模型/JSON 出问题就走确定性圣经
            print(f"[session0] 生成失败，用确定性圣经：{type(exc).__name__}: {exc}")
    if bible is None:
        bible = _fallback(meta, store.title)
    bible["kb_meta"] = {k: meta[k] for k in ("topics", "monsters", "skills", "rules")}

    # 落库：世界层记圣经 + 开场场景
    if world is not None:
        world.set("bible", json.dumps(bible, ensure_ascii=False))
        sc = world.scene()
        sid = sc["id"] if sc else "sandbox"
        if sc is None:
            world.add_scene(sid, "开场", bible["place"], bible["scene"], source="seed")
            world.enter(sid)
        else:
            world.con.execute(
                "UPDATE scene SET place=?, description=?, label=? WHERE id=?",
                (bible["place"], bible["scene"], "开场", sid))
            world.set("scene", sid)
            world.con.commit()

    # 剧本对象也更新（房间的 state 是从这里读的）
    try:
        sc = store.scenes.get(store.start_scene)
        if sc is not None:
            sc["public_text"] = bible["scene"]
            sc["location"] = bible["place"]
            sc["dm_notes"] = bible["secret"]
            if not sc.get("keywords"):
                sc["keywords"] = bible["hooks"][:4]
    except Exception:  # noqa: BLE001
        pass
    return bible


def summary_text(bible: dict) -> str:
    """给玩家看的开局说明（不掺内幕）。"""
    bits = [f"【开局】{bible.get('era')} · {bible.get('place')}",
            f"基调：{bible.get('tone')}" if bible.get("tone") else "",
            f"事件：{bible.get('premise')}" if bible.get("premise") else ""]
    return "\n".join(b for b in bits if b)
