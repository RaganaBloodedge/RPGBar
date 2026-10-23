"""阶段二：把五类容器物化出来（docs/知识库目标结构.md 的实现）。

```
证据层（block + 通用 entity/fact）──► 五个编译器 ──► 五类出口产物（下面这些表）
                                              │
                                    rule_subtype / rule_flow
                                    pc_card_schema / pc_skill
                                    item（名字索引）
                                    npc（怪物/人 档案）
                                    lore_topic + lore_member（摘要 + 原文聚类）
```

分工原则和切分那一层一致：**判断归模型，拼装归规则。**

- 规则子类名、判定步骤、世界观摘要：语义判断，问模型（并且挡住"引文不在原文"的编造）；
- 技能/属性/物品/怪物档案：书里已经有结构化事实（fact/field），这里只做**投影**——
  把 `monster.STR=65` 投影成 `npc_field(key=STR, value=65)`，不再让模型重说一遍；
- 流程图：由判定步骤**拼**出来（节点=步骤，边=顺序），不是让模型画。

这样五张产物表都能追溯到 `block_id` 与 `quote`，运行期 DM 查的是成品，不是现拼。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

from . import db

# ---------------------------------------------------------------- 小工具


def _norm(s: str) -> str:
    return re.sub(r"[\s　]+", "", s or "")


def _quote_ok(quote: str, text: str) -> bool:
    q, t = _norm(quote), _norm(text)
    return bool(q) and len(q) >= 4 and q in t


def _json(d) -> str:
    return json.dumps(d, ensure_ascii=False)


def _reset(con, book_id: str) -> None:
    """清掉这本书的容器产物（重跑幂等）。没有 book_id 列的表按子查询清。"""
    for t in ("rule_entry", "rule_subtype", "rule_flow", "pc_attribute", "pc_card_schema",
              "pc_skill", "pc_relation", "item", "npc", "lore_topic"):
        try:
            con.execute(f"DELETE FROM {t} WHERE book_id=?", (book_id,))
        except sqlite3.OperationalError:
            pass
    for t, key, parent in (("rule_step", "rule_id", "rule_entry"),
                           ("item_field", "item_id", "item"),
                           ("npc_field", "npc_id", "npc"),
                           ("npc_ability", "npc_id", "npc"),
                           ("lore_member", "topic_id", "lore_topic")):
        try:
            con.execute(f"DELETE FROM {t} WHERE {key} NOT IN (SELECT id FROM {parent})")
        except sqlite3.OperationalError:
            pass


def _ask(model, prompt: str, max_tokens: int = 800) -> dict | None:
    if model is None:
        return None
    from .segment import SegmentError, invoke

    try:
        return invoke(model, prompt, attempts=4, label="containers", max_tokens=max_tokens)
    except SegmentError as exc:
        print(f"[containers] 模型没给出结果：{exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- ① 规则


_SUBTYPE_PROMPT = """下面是一本跑团规则书里**同一章**（{chapter}）的规则条目（编号|标题）。
请按"讲的是哪一类规则"给这一章分 **1–3 个子类**：

- 每组一个 2–6 字的组名（战斗、伤害治疗、射击、追逐、理智、魔法、技能检定、旅行…）；
- 每个条目只进一组；拿不准的放进"其他"；
- 组名要具体到"判定对象/流程"，不要用"规则一""其他规则"。

只输出**一行** JSON，键是组名、值是这个组包含的编号：

{"射击":[1,4,9],"伤害治疗":[2,3,5]}

{items}"""


def _rule_subtypes(con, book_id: str, model) -> dict[str, str]:
    """规则 → 子类。

    **先用书自己的结构分组（章），再让模型给每一章起 1–3 个子类名。**
    早先把 60 条标题一口气丢给 4B，它只会给出"战斗/技能检定/其他"三个大筐 —— 粒度太粗；
    按章分开问、每组只有十几条，它就能给出"伤害治疗""射击""追逐"这种真正有用的子类。
    """
    rows = con.execute(
        "SELECT e.id,e.title,s.path FROM rule_entry e LEFT JOIN block b ON b.id=e.block_id "
        "LEFT JOIN section s ON s.id=b.section_id WHERE e.book_id=? AND e.title<>'' "
        "ORDER BY e.page", (book_id,),
    ).fetchall()
    if not rows:
        return {}
    by_chapter: dict[str, list] = defaultdict(list)
    for r in rows:
        chapter = (r["path"] or "").split(" > ")[0] or "其他"
        by_chapter[chapter].append(r)

    group_of: dict[str, str] = {}
    names: dict[str, int] = {}
    for chapter, items in by_chapter.items():
        chunk = items[:60]
        listing = "\n".join(f"{k + 1}|{r['title'][:40]}" for k, r in enumerate(chunk))
        data = _ask(model, _SUBTYPE_PROMPT.replace("{chapter}", chapter).replace("{items}", listing),
                    max_tokens=1000)
        mapping: dict[str, list] = {}
        if isinstance(data, dict):
            mapping = data.get("groups") if isinstance(data.get("groups"), dict) else data
        for name, idxs in (mapping or {}).items():
            if not isinstance(idxs, list):
                continue
            name = str(name).strip()[:12]
            if not name or name in ("groups", "items"):
                continue
            names[name] = names.get(name, 0) + len(idxs)
            for k in idxs:
                try:
                    i = int(k) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(chunk):
                    group_of[chunk[i]["id"]] = name
        if not mapping:
            # 模型没给 → 用章名兜底（仍然是"从书里长出来"的名字）
            names[chapter[:12]] = names.get(chapter[:12], 0) + len(chunk)
            for r in chunk:
                group_of.setdefault(r["id"], chapter[:12])

    con.execute("DELETE FROM rule_subtype WHERE book_id=?", (book_id,))
    for name, n in names.items():
        sid = "rs:" + re.sub(r"\W+", "", name)[:20]
        con.execute(
            "INSERT OR REPLACE INTO rule_subtype(id,book_id,name,parent_id,evidence,rules) "
            "VALUES(?,?,?,?,?,?)",
            (sid, book_id, name, None, "模型按章聚类", n),
        )
    for rid, name in group_of.items():
        sid = "rs:" + re.sub(r"\W+", "", name)[:20]
        con.execute("UPDATE rule_entry SET subtype_id=? WHERE id=?", (sid, rid))
    return group_of


_STEP_PROMPT = """把下面这条规则拆成**有序判定步骤**（DM 照着一步步跑）。

- 每步：kind = step（普通步骤）/ branch（条件分支）/ outcome（结果）；
- text 不超过 30 字，用你自己的话概括**这一步要做什么**；
- quote 逐字摘自原文（至少 6 字），我会用它核对，抄错这一步就作废；
- 没有明确步骤的规则，输出 {"steps":[]}，不要硬凑。

只输出 JSON：{"steps":[{"kind":"step","text":"…","quote":"…"}]}

标题：{title}
```
{text}
```"""

_STEP_MARK = re.compile(r"(^\s*(?:\d{1,2}[.、)]|步骤\s*\d|第[一二三四五六七八九十]+步)|如果|否则|若)", re.M)


def _rule_steps(con, book_id: str, model) -> int:
    rows = con.execute(
        "SELECT id,block_id,title,text FROM rule_entry WHERE book_id=? AND length(text)>60",
        (book_id,),
    ).fetchall()
    # 只对**看起来有步骤**的规则问模型（没有步骤的规则本来就不需要流程图）
    cand = [r for r in rows if len(_STEP_MARK.findall(r["text"] or "")) >= 2][:120]
    con.execute("DELETE FROM rule_step WHERE rule_id IN (SELECT id FROM rule_entry WHERE book_id=?)",
                (book_id,))
    con.execute("DELETE FROM rule_flow WHERE rule_id IN (SELECT id FROM rule_entry WHERE book_id=?)",
                (book_id,))
    made = 0
    for r in cand:
        prompt = (_STEP_PROMPT.replace("{title}", r["title"] or "")
                  .replace("{text}", (r["text"] or "")[:900]))
        data = _ask(model, prompt, max_tokens=700)
        if not data:
            continue
        steps = []
        for s in data.get("steps") or []:
            quote = str(s.get("quote") or "").strip()
            kind = str(s.get("kind") or "step").strip()
            if kind not in ("step", "branch", "outcome"):
                kind = "step"
            if not _quote_ok(quote, r["text"] or ""):
                continue  # 编的引文直接丢
            steps.append((kind, str(s.get("text") or "")[:60], quote[:120]))
        if not steps:
            continue
        for k, (kind, text, quote) in enumerate(steps):
            con.execute(
                "INSERT INTO rule_step(rule_id,seq,kind,text,quote,block_id) VALUES(?,?,?,?,?,?)",
                (r["id"], k, kind, text, quote, r["block_id"]),
            )
            made += 1
        # 流程图 = 步骤串联（拼装，不是判断）；有 branch 的标成 decision
        nodes = [{"id": f"n{k}", "type": kind, "text": text}
                 for k, (kind, text, _q) in enumerate(steps)]
        edges = [{"from": f"n{k}", "to": f"n{k + 1}", "label": ""} for k in range(len(steps) - 1)]
        con.execute(
            "INSERT OR REPLACE INTO rule_flow(rule_id,block_id,kind,nodes,edges,confidence) "
            "VALUES(?,?,?,?,?,?)",
            (r["id"], r["block_id"],
             "decision" if any(s[0] == "branch" for s in steps) else "sequence",
             _json(nodes), _json(edges), 0.7),
        )
    return made


def build_rule(con, book_id: str, model) -> dict:
    rows = con.execute(
        "SELECT id,title,text,page FROM block WHERE book_id=? AND kind='rule' "
        "AND COALESCE(canonical,1)=1 ORDER BY page,ord", (book_id,),
    ).fetchall()
    con.execute("DELETE FROM rule_entry WHERE book_id=?", (book_id,))
    for b in rows:
        con.execute(
            "INSERT INTO rule_entry(id,book_id,block_id,title,subtype_id,text,page) VALUES(?,?,?,?,?,?,?)",
            (b["id"], book_id, b["id"], (b["title"] or "")[:60], None, (b["text"] or "")[:4000],
             b["page"]),
        )
    groups = _rule_subtypes(con, book_id, model)
    steps = _rule_steps(con, book_id, model)
    flows = con.execute(
        "SELECT COUNT(*) c FROM rule_flow WHERE rule_id IN (SELECT id FROM rule_entry WHERE book_id=?)",
        (book_id,),
    ).fetchone()["c"]
    subtypes = con.execute("SELECT COUNT(*) c FROM rule_subtype WHERE book_id=?",
                           (book_id,)).fetchone()["c"]
    return {"rules": len(rows), "subtypes": subtypes, "grouped": len(groups),
            "steps": steps, "flows": flows}


# ---------------------------------------------------------------- ② 玩家属性


def build_pc(con, book_id: str) -> dict:
    # 技能：实体 + 基础值 fact + 块里的描述
    skills = con.execute(
        "SELECT e.id,e.name,e.block_id,e.page,b.text FROM entity e LEFT JOIN block b ON b.id=e.block_id "
        "WHERE e.book_id=? AND e.etype='skill'", (book_id,),
    ).fetchall()
    con.execute("DELETE FROM pc_skill WHERE book_id=?", (book_id,))
    for e in skills:
        base = con.execute(
            "SELECT raw FROM fact WHERE entity_id=? AND (field_key LIKE '%基础值%' OR field_key LIKE '%初始%') "
            "ORDER BY id LIMIT 1", (e["id"],),
        ).fetchone()
        raw = re.sub(r"\s+", " ", (e["text"] or ""))
        name = re.sub(r"\s+", "", e["name"] or "")
        desc = raw[:300]
        # 一行一技能时（技能列表最常见的版式）：只取"这条技能名那一行"及其后续描述行，
        # 到下一条 `名称（NN%）` 为止 —— 不把整张技能表当成它的描述。
        lines = [x.strip() for x in (e["text"] or "").split("\n") if x.strip()]
        idx = next((i for i, ln in enumerate(lines) if _norm(ln).startswith(name)), None)
        if idx is not None:
            seg = [lines[idx]]
            for ln in lines[idx + 1:]:
                if re.match(r"^[\u4e00-\u9fa5A-Za-z·（）()]{2,14}[（(][^）)]{0,10}[）)]", ln) and "%" in ln:
                    break
                seg.append(ln)
            desc = " ".join(seg)[:300]
            if not base and "%" in lines[idx]:
                m = re.search(r"[（(]\s*(\d{1,3}%)\s*[）)]", lines[idx])
                if m:
                    base = {"raw": m.group(1)}
        con.execute(
            "INSERT OR REPLACE INTO pc_skill(id,book_id,block_id,name,base_value,description,page) "
            "VALUES(?,?,?,?,?,?,?)",
            ("skill:" + _norm(e["name"])[:40], book_id, e["block_id"], e["name"],
             (base["raw"] if base else ""), desc, e["page"]),
        )

    # 属性 / 状态：**以属性实体为单位**（力量（STR）/ 理智 …），不是以 fact 字段为单位。
    # 早先用 fact.field_key 当 key，8 个属性全变成同一个"缩写"字段 —— 卡上只剩一行。
    con.execute("DELETE FROM pc_attribute WHERE book_id=?", (book_id,))
    for e in con.execute(
        "SELECT e.id,e.name,e.block_id,e.page,f.raw,f.quote FROM entity e "
        "LEFT JOIN fact f ON f.entity_id=e.id AND f.id=(SELECT MIN(id) FROM fact WHERE entity_id=e.id) "
        "WHERE e.book_id=? AND e.etype='attribute' ORDER BY e.page,e.name", (book_id,),
    ):
        con.execute(
            "INSERT INTO pc_attribute(book_id,block_id,key,label,value,unit,quote) VALUES(?,?,?,?,?,?,?)",
            (book_id, e["block_id"], e["name"], e["name"], e["raw"] or "", "", e["quote"] or ""),
        )

    # 角色卡模板：**从书里学出来的字段**，三路合并
    con.execute("DELETE FROM pc_card_schema WHERE book_id=?", (book_id,))
    cards: dict[str, dict] = {}
    for f in con.execute(
        "SELECT key,label,kind,first_page,evidence_block,evidence_quote FROM field "
        "WHERE scope IN ('pc','attribute') ORDER BY COALESCE(first_page,9999), key"
    ):
        cards[f["key"]] = {"label": f["label"] or f["key"], "kind": f["kind"] or "text",
                           "block": f["evidence_block"] or "", "quote": f["evidence_quote"] or "",
                           "first": f["first_page"] or 9999}
    for r in con.execute(
        "SELECT key,label,MIN(quote) quote,MIN(block_id) block,MIN(id) first FROM pc_attribute "
        "WHERE book_id=? GROUP BY key ORDER BY first", (book_id,)
    ):
        key = (r["key"] or "").strip()
        if not key:
            continue
        cards.setdefault(key, {"label": r["label"] or key, "kind": "text",
                               "block": r["block"] or "", "quote": r["quote"] or "",
                               "first": r["first"] or 9999})
    for r in con.execute(
        "SELECT field_key,MIN(quote) quote,MIN(block_id) block FROM fact "
        "WHERE entity_id IN (SELECT id FROM entity WHERE book_id=? AND etype='attribute') "
        "GROUP BY field_key", (book_id,)
    ):
        k = (r["field_key"] or "").replace("attribute.", "pc.")
        cards.setdefault(k, {"label": k.split(".")[-1], "kind": "text",
                             "block": r["block"] or "", "quote": r["quote"] or "", "first": 9999})
    fields = sorted(cards.items(), key=lambda kv: (kv[1]["first"], kv[0]))
    for k, (key, f) in enumerate(fields):
        con.execute(
            "INSERT OR REPLACE INTO pc_card_schema(book_id,field_key,label,kind,ord,evidence_block,evidence_quote) "
            "VALUES(?,?,?,?,?,?,?)",
            (book_id, key, f["label"], f["kind"], k, f["block"], f["quote"]),
        )

    # 关系图：正则只兜"是…的…"这一种句式；真正的抽取在 build_relations（模型判）
    con.execute("DELETE FROM pc_relation WHERE book_id=?", (book_id,))
    rel = 0
    for b in con.execute("SELECT id,text FROM block WHERE book_id=? AND kind IN ('npc','lore')",
                         (book_id,)):
        for m in re.finditer(
            r"([\u4e00-\u9fa5·]{2,10})是([\u4e00-\u9fa5·]{2,10})的"
            r"(儿子|女儿|妻子|丈夫|父亲|母亲|兄弟|姐妹|学生|老师|仆人|主人|手下|上司)",
            b["text"] or "",
        ):
            con.execute(
                "INSERT INTO pc_relation(book_id,src,dst,rel,block_id,quote) VALUES(?,?,?,?,?,?)",
                (book_id, m.group(1), m.group(2), m.group(3), b["id"], m.group(0)),
            )
            rel += 1
    return {"skills": len(skills),
            "attributes": con.execute("SELECT COUNT(*) c FROM pc_attribute WHERE book_id=?",
                                      (book_id,)).fetchone()["c"],
            "card_fields": len(fields), "relations": rel}


_REL_PROMPT = """下面这段跑团文本里出现了这些人物/怪物：{names}

请抽出它们之间**原文明示**的关系（不要推断、不要脑补）。

- src / dst 用上面给出的名字（原文写法）；
- rel 用 2–6 个字（父子、夫妻、主仆、同僚、敌对、侍奉、崇拜、创造、召唤、附身…）；
- quote 逐字摘自原文（至少 6 字），我会核对，抄错这条作废；
- 没有明示关系就输出 {"relations":[]}。

只输出 JSON：{"relations":[{"src":"哈维","dst":"卡特","rel":"同僚","quote":"哈维和卡特一起调查"}]}

原文：
```
{text}
```"""


def build_relations(con, book_id: str, model) -> int:
    """关系图：**模型读原文抽明示关系**，逐条验证引文。

    为什么不再靠正则：正则只能覆盖"是…的…"这一种句式（全库只命中 1 条）。
    这里先用"已认出的人名/怪物名"做候选筛选（几何：这段确实同时提到了两个已知实体），
    再让模型判它们是什么关系 —— 抽出来的是"谁和谁、什么关系、原文哪一句"。
    """
    con.execute("DELETE FROM pc_relation WHERE book_id=?", (book_id,))
    rows_all = con.execute("SELECT name FROM entity WHERE book_id=? AND etype IN ('npc','monster') "
                           "AND length(name)>=2", (book_id,)).fetchall()
    names = [r["name"] for r in rows_all]
    # 通用称谓（"调查员""守秘人"）不是关系图里的节点：出现在 >3% 块里的名字一律不当端点
    total_blocks = max(1, con.execute("SELECT COUNT(*) c FROM block WHERE book_id=?",
                                      (book_id,)).fetchone()["c"])
    if names:
        keep = []
        for nm in names:
            hit = con.execute("SELECT COUNT(*) c FROM block WHERE book_id=? AND text LIKE ?",
                              (book_id, f"%{nm}%")).fetchone()["c"]
            if hit <= max(30, int(total_blocks * 0.03)):
                keep.append(nm)
        names = keep
    if not model or not names:
        return 0
    rows = con.execute(
        "SELECT id,title,text FROM block WHERE book_id=? AND kind IN ('npc','lore','pc') "
        "AND length(text) BETWEEN 60 AND 1500", (book_id,),
    ).fetchall()
    cand = []
    for b in rows:
        t = b["text"] or ""
        hit = [n for n in names if n in t]
        if len(hit) >= 2:
            cand.append((b["id"], b["title"] or "", t, hit[:6]))
    seen: set[tuple[str, str, str]] = set()
    made = 0
    for i in range(0, len(cand), 4):
        chunk = cand[i : i + 4]
        for bid, title, text, hit in chunk:
            data = _ask(model, _REL_PROMPT.replace("{names}", "、".join(hit))
                        .replace("{text}", text[:800]), max_tokens=400)
            if not data:
                continue
            for r in data.get("relations") or []:
                src = str(r.get("src") or "").strip()[:20]
                dst = str(r.get("dst") or "").strip()[:20]
                rel = str(r.get("rel") or "").strip()[:8]
                quote = str(r.get("quote") or "").strip()
                if not (src and dst and rel) or src == dst:
                    continue
                if src not in hit or dst not in hit:
                    continue  # 端点必须是这一段里已认出的人/怪
                if not _quote_ok(quote, text):
                    continue  # 引文编的
                if src not in quote or dst not in quote:
                    continue  # 引文必须同时提到两端，否则"某某→某某"是模型脑补的
                key = (src, dst, rel)
                if key in seen:
                    continue
                seen.add(key)
                con.execute(
                    "INSERT INTO pc_relation(book_id,src,dst,rel,block_id,quote) VALUES(?,?,?,?,?,?)",
                    (book_id, src, dst, rel, bid, quote[:160]),
                )
                made += 1
    return made


# ---------------------------------------------------------------- ③ 工具 / 物品


def build_tool(con, book_id: str) -> dict:
    items = con.execute(
        "SELECT id,name,aliases,summary,block_id,page FROM entity WHERE book_id=? AND etype='item'",
        (book_id,),
    ).fetchall()
    con.execute("DELETE FROM item WHERE book_id=?", (book_id,))
    con.execute("DELETE FROM item_field WHERE item_id NOT IN (SELECT id FROM item)")
    seen: set[str] = set()
    made = 0
    for e in items:
        norm = _norm(e["name"])[:40]
        if not norm or norm in seen:
            continue
        seen.add(norm)
        iid = "item:" + norm
        con.execute(
            "INSERT OR REPLACE INTO item(id,book_id,block_id,name,name_norm,aliases,summary,page) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (iid, book_id, e["block_id"], e["name"], norm, e["aliases"] or "[]",
             (e["summary"] or "")[:200], e["page"]),
        )
        made += 1
        for f in con.execute(
            "SELECT field_key,raw,unit,quote FROM fact WHERE entity_id=?", (e["id"],)
        ):
            con.execute(
                "INSERT INTO item_field(item_id,key,value,unit,quote) VALUES(?,?,?,?,?)",
                (iid, (f["field_key"] or "").split(".")[-1], f["raw"], f["unit"] or "",
                 f["quote"] or ""),
            )
    return {"items": made}


# ---------------------------------------------------------------- ④ NPC


_ABILITY_KEYS = ("特殊能力", "法术", "攻击", "战斗方式", "每回合攻击", "技能", "能力",
                 "理智丧失", "理智损失", "护甲")


def build_npc(con, book_id: str) -> dict:
    ents = con.execute(
        "SELECT id,name,etype,aliases,summary,block_id,page FROM entity "
        "WHERE book_id=? AND etype IN ('monster','npc')", (book_id,),
    ).fetchall()
    con.execute("DELETE FROM npc WHERE book_id=?", (book_id,))
    con.execute("DELETE FROM npc_field WHERE npc_id NOT IN (SELECT id FROM npc)")
    con.execute("DELETE FROM npc_ability WHERE npc_id NOT IN (SELECT id FROM npc)")
    made = 0
    for e in ents:
        etype = "monster" if e["etype"] == "monster" else "human"
        nid = "npc:" + _norm(e["name"])[:40]
        con.execute(
            "INSERT OR REPLACE INTO npc(id,book_id,block_id,etype,name,aliases,summary,page,readonly) "
            "VALUES(?,?,?,?,?,?,?,?,1)",
            (nid, book_id, e["block_id"], etype, e["name"], e["aliases"] or "[]",
             (e["summary"] or "")[:200], e["page"]),
        )
        made += 1
        for f in con.execute(
            "SELECT field_key,raw,quote FROM fact WHERE entity_id=?", (e["id"],)
        ):
            key = (f["field_key"] or "").split(".")[-1]
            if key.endswith("·掷骰"):
                continue  # 掷骰并进对应字段的 dice 列
            if any(k in key for k in _ABILITY_KEYS):
                con.execute(
                    "INSERT INTO npc_ability(npc_id,name,text,quote) VALUES(?,?,?,?)",
                    (nid, key, f["raw"], f["quote"] or ""),
                )
                continue
            dice = con.execute(
                "SELECT raw FROM fact WHERE entity_id=? AND field_key=?",
                (e["id"], (f["field_key"] or "") + "·掷骰"),
            ).fetchone()
            con.execute(
                "INSERT INTO npc_field(npc_id,key,value,dice,quote) VALUES(?,?,?,?,?)",
                (nid, key, f["raw"], (dice["raw"] if dice else ""), f["quote"] or ""),
            )
    humans = con.execute("SELECT COUNT(*) c FROM npc WHERE book_id=? AND etype='human'",
                         (book_id,)).fetchone()["c"]
    return {"npcs": made, "monsters": made - humans, "humans": humans,
            "fields": con.execute("SELECT COUNT(*) c FROM npc_field").fetchone()["c"],
            "abilities": con.execute("SELECT COUNT(*) c FROM npc_ability").fetchone()["c"]}


# ---------------------------------------------------------------- ⑤ 世界观


_TOPIC_PROMPT = """下面是一本跑团规则/模组里的若干段原文（同一主题的候选）。
请给这个主题起一个 2–8 字的**名称**，并用一句话（≤40 字）概括它讲的是什么。

- 只概括这些段落共同说的事，不要编；
- 如果这些段落其实不是同一主题，name 填 ""。

只输出 JSON：{"name":"克苏鲁","summary":"旧日支配者之一，沉睡在海底都市拉莱耶。"}

段落：
```
{text}
```"""


def _vectors(con, book_id: str) -> dict[str, list[float]]:
    try:
        import numpy as np
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[float]] = {}
    for r in con.execute(
        "SELECT v.block_id,v.vec FROM block_vec v JOIN block b ON b.id=v.block_id WHERE b.book_id=?",
        (book_id,),
    ):
        out[r["block_id"]] = np.frombuffer(r["vec"], dtype=np.float32).tolist()
    return out


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


def build_lore(con, book_id: str, model, threshold: float = 0.66) -> dict:
    """世界观：**一个主题 = 一节的 lore 内容**，摘要由模型读这一节写。

    为什么不按"全局语义聚类"：早先按向量阈值全局聚类，产出 211 个主题、名字重复、
    摘要和成员对不上（"克苏鲁"下面挂着模组建议）。书自己的章节就是主题边界，
    用它当骨架、再按同名主题合并，才是稳定可查的。

    - 主题名 = 节标题（去掉编号）；
    - 摘要 = 模型读这一节最多 1200 字写一句（没有模型时用块自带摘要）；
    - 成员 = 这一节的 lore 块，`similarity` = 与本节质量心的余弦（1.0 表示居中）。
    """
    rows = con.execute(
        "SELECT b.id,b.section_id,b.title,b.text,b.page,b.summary,s.title st,s.path sp "
        "FROM block b LEFT JOIN section s ON s.id=b.section_id "
        "WHERE b.book_id=? AND b.kind='lore' ORDER BY b.page,b.ord", (book_id,),
    ).fetchall()
    vecs = _vectors(con, book_id)
    con.execute("DELETE FROM lore_topic WHERE book_id=?", (book_id,))
    con.execute("DELETE FROM lore_member WHERE topic_id NOT IN (SELECT id FROM lore_topic)")

    by_sec: dict[str, list] = defaultdict(list)
    for r in rows:
        by_sec[r["section_id"] or ""].append(r)

    topics: dict[str, dict] = {}
    for sec_id, group in by_sec.items():
        title = (group[0]["st"] or group[0]["title"] or "").strip()
        label = re.sub(r"^[\d.、\s]+", "", title)[:20] or "未命名主题"
        joined = "\n---\n".join((r["text"] or "")[:300] for r in group)[:1200]
        data = _ask(model, _TOPIC_PROMPT.replace("{text}", joined), max_tokens=220) if model else None
        name = (data or {}).get("name") or ""
        summary = (data or {}).get("summary") or ""
        if not summary:
            summary = (group[0]["summary"] or re.sub(r"\s+", "", group[0]["text"] or "")[:60])
        # 摘要必须**真的来自这些段落**：4B 常写一句放之四海皆准的"克苏鲁神话…"。
        # 判据：摘要里的三字片段有多少能在这批原文里找到；太低就退回原文摘要。
        if summary and _overlap(summary, joined) < 0.2:
            summary = (group[0]["summary"] or re.sub(r"\s+", "", group[0]["text"] or "")[:60])
        # 主题名用**节标题**：模型给的名常常所有节都叫"克苏鲁"，按它合并会把整本书并成一个主题。
        key = _norm(label)[:20]
        # 同一个主题名在别的章也出现 → 并到同一个摘要下（"语义相似"的落点）
        if key in topics:
            topics[key]["members"].extend(group)
            if len(summary) > len(topics[key]["summary"]):
                topics[key]["summary"] = summary
            continue
        topics[key] = {"label": label[:20], "summary": summary[:200],
                       "members": list(group), "seed": group[0]["id"]}

    made = 0
    for k, (key, t) in enumerate(topics.items()):
        # 节内相似度：成员对本节质心的余弦（给 DM/审计看"这条挂得远不远"）
        vs = [vecs.get(r["id"]) for r in t["members"]]
        vs = [v for v in vs if v]
        if vs:
            dim = len(vs[0])
            centroid = [sum(v[i] for v in vs) / len(vs) for i in range(dim)]
        else:
            centroid = None
        tid = f"lore:{k:04d}"
        con.execute(
            "INSERT INTO lore_topic(id,book_id,summary,label,block_id,members) VALUES(?,?,?,?,?,?)",
            (tid, book_id, t["summary"], t["label"], t["seed"], len(t["members"])),
        )
        made += 1
        for r in t["members"]:
            v = vecs.get(r["id"])
            sim = round(_cos(v, centroid), 4) if (v and centroid) else 1.0
            con.execute(
                "INSERT INTO lore_member(topic_id,block_id,similarity,added_at) VALUES(?,?,?,?)",
                (tid, r["id"], sim, datetime.now().isoformat(timespec="seconds")),
            )
    return {"lore_blocks": len(rows), "topics": made,
            "clustered": sum(1 for t in topics.values() if len(t["members"]) > 1)}


# ---------------------------------------------------------------- 总入口


def build_all(kb_path: str, book_id: str, *, model=None, lore_threshold: float = 0.66) -> dict:
    con = db.connect(kb_path)  # 顺带把新表建出来（CREATE TABLE IF NOT EXISTS）
    con.row_factory = sqlite3.Row
    _reset(con, book_id)
    stats = {
        "rule": build_rule(con, book_id, model),
        "pc": build_pc(con, book_id),
        "tool": build_tool(con, book_id),
        "npc": build_npc(con, book_id),
        "lore": build_lore(con, book_id, model, lore_threshold),
    }
    # 关系图最后抽：它要用刚建好的 npc/monster 名字当候选
    stats["relations"] = {"relations": build_relations(con, book_id, model)}
    con.commit()
    return stats


def _overlap(summary: str, text: str) -> float:
    """摘要的三字片段在原文里出现的比例（判断摘要是不是跑题了）。"""
    a = _norm(summary)
    b = _norm(text)
    if len(a) < 6 or not b:
        return 1.0
    grams = [a[i:i + 3] for i in range(0, len(a) - 2)]
    hit = sum(1 for g in grams if g in b)
    return hit / max(1, len(grams))
