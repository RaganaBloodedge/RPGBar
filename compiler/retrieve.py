"""检索层：DM 侧唯一需要的入口。**永远返回原文**，摘要只当钩子。

三路召回，按场景分工：

| 调用 | 走哪条路 | 为什么 |
|---|---|---|
| `item("燧发手枪")` / `skill("考古学")` / `npc("拜亚基")` | **名字索引**（精确 + 别名） | 玩家说得出名字的时候，语义检索是多余的，还会召回同名的无关段落 |
| `search("理智检定失败会怎样")` | BM25 + 向量混合 | 玩家用自然语言描述的时候，需要模糊匹配 |
| `rule("战斗")` + `flow()` | 规则实体 + 流程图 | 规则要的是"步骤和分支"，不是"最像的一段话" |

时间维度单独一层：`search(..., year=1925)` 会按块的 `valid_from/valid_to` 过滤。
这是"时间 RAG"最要紧的一半 —— 不然玩家问 1925 年的水库，可能召回 1920 年的旧描述，
而且**召回得越准，错得越狠**（这正是原来纯 RAG 方案翻车的地方）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

import jieba
import numpy as np

from . import db
from .embedder import Embedder


def tokenize(text: str) -> list[str]:
    text = re.sub(r"[\s，。！？、；：,.!?;:“”‘’（）()【】\[\]]+", " ", text or "")
    return [t for t in jieba.lcut(text) if t.strip()] or [text or ""]


@dataclass
class Hit:
    block_id: str
    title: str
    text: str
    page: int
    kind: str
    subtype: str
    section: str
    score: float
    why: str

    def brief(self, n: int = 300) -> str:
        body = re.sub(r"\s+", " ", self.text or "")
        return f"[p{self.page}｜{self.section}｜{self.title}] {body[:n]}"


class KB:
    def __init__(self, path: str, with_vec: bool = True):
        self.con = db.connect(path)
        self.embedder = Embedder() if with_vec else None
        self._vec_ids: list[str] = []
        self._vec: np.ndarray | None = None

    def close(self) -> None:
        self.con.close()

    # ---------------------------------------------------------------- 块检索

    def _load_vecs(self) -> None:
        if self._vec is not None or not (self.embedder and self.embedder.available):
            return
        rows = self.con.execute("SELECT block_id, dim, vec FROM block_vec").fetchall()
        if not rows:
            self._vec_ids, self._vec = [], None
            return
        self._vec_ids = [r["block_id"] for r in rows]
        self._vec = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])

    def search(
        self, query: str, k: int = 8, kinds: list[str] | None = None, year: int | None = None,
    ) -> list[Hit]:
        toks = tokenize(query)
        if not toks:
            return []
        match = " OR ".join(f'"{t}"' for t in toks if t)
        rows = self.con.execute(
            "SELECT block_id, bm25(block_fts) AS s FROM block_fts WHERE block_fts MATCH ? "
            "ORDER BY s LIMIT 80",
            (match,),
        ).fetchall()
        bm25 = {r["block_id"]: -r["s"] for r in rows}

        cos: dict[str, float] = {}
        self._load_vecs()
        if self._vec is not None and self.embedder:
            q = self.embedder.encode([query], is_query=True)
            if q is not None:
                sims = (self._vec @ q[0]).astype(float)
                top = np.argsort(-sims)[:80]
                cos = {self._vec_ids[i]: float(sims[i]) for i in top if sims[i] > 0.2}

        ids = set(bm25) | set(cos)
        if not ids:
            return []
        bmax = max(bm25.values()) if bm25 else 1.0
        out: list[Hit] = []
        for bid in ids:
            row = self.con.execute(
                """SELECT b.*, COALESCE(s.path,'') path FROM block b
                   LEFT JOIN section s ON s.id=b.section_id WHERE b.id=?""",
                (bid,),
            ).fetchone()
            if not row:
                continue
            if kinds and row["kind"] not in kinds:
                continue
            if year is not None:
                vf, vt = row["valid_from"], row["valid_to"]
                if (vf is not None and year < vf) or (vt is not None and year > vt):
                    continue
            score = 0.6 * (bm25.get(bid, 0.0) / bmax) + 0.4 * cos.get(bid, 0.0)
            why = []
            if bid in bm25:
                why.append("bm25")
            if bid in cos:
                why.append("vec")
            if query and row["title"] and query.strip() in row["title"]:
                score += 0.25
                why.append("标题命中")
            out.append(
                Hit(bid, row["title"], row["text"], row["page"], row["kind"] or "",
                    row["subtype"] or "", row["path"] or "", round(score, 4), "+".join(why))
            )
        out.sort(key=lambda h: -h.score)
        return out[:k]

    # ---------------------------------------------------------------- 名字索引

    def entity(self, name: str, etype: str | None = None) -> dict | None:
        norm = re.sub(r"\s+", "", name or "")
        if not norm:
            return None
        sql = "SELECT * FROM entity WHERE name_norm=?"
        args: list = [norm]
        if etype:
            sql += " AND etype=?"
            args.append(etype)
        row = self.con.execute(sql + " ORDER BY confidence DESC LIMIT 1", args).fetchone()
        if not row:  # 别名 / 包含关系（"燧发手枪" vs "燧发手枪（单管）"）
            sql2 = "SELECT * FROM entity WHERE (name_norm LIKE ? OR aliases LIKE ?)"
            args2: list = [f"%{norm}%", f"%{norm}%"]
            if etype:
                sql2 += " AND etype=?"
                args2.append(etype)
            row = self.con.execute(sql2 + " ORDER BY length(name) LIMIT 1", args2).fetchone()
        return dict(row) if row else None

    def card(self, name: str, etype: str | None = None) -> dict:
        """动态卡片：字段来自字段字典，**不是固定列**。"""
        from .schema_skill import SchemaSkill

        e = self.entity(name, etype)
        if not e:
            return {"error": f"没有找到「{name}」", "hint": "试试 kb_search 模糊检索"}
        schema = SchemaSkill(self.con)
        card = schema.card(e["id"])
        # 原文：DM 判断时最想看的还是书里怎么写的
        if e.get("block_id"):
            b = self.con.execute("SELECT text, page, title FROM block WHERE id=?", (e["block_id"],)).fetchone()
            if b:
                card["source"] = {"title": b["title"], "page": b["page"], "text": b["text"][:1500]}
        return card

    def flow(self, rule: str) -> dict | None:
        e = self.entity(rule)
        if not e:
            row = self.con.execute(
                "SELECT f.* FROM flow f JOIN entity en ON en.id=f.rule_id WHERE en.name LIKE ? LIMIT 1",
                (f"%{rule}%",),
            ).fetchone()
        else:
            row = self.con.execute("SELECT * FROM flow WHERE rule_id=?", (e["id"],)).fetchone()
        if not row:
            return None
        return {
            "rule": rule,
            "kind": row["kind"],
            "nodes": json.loads(row["nodes"]),
            "edges": json.loads(row["edges"]),
            "confidence": row["confidence"],
        }

    def items_like(self, q: str, limit: int = 10) -> list[dict]:
        rows = self.con.execute(
            "SELECT name, page FROM entity WHERE etype IN ('item') AND (name LIKE ? OR name_norm LIKE ?) "
            "ORDER BY length(name) LIMIT ?",
            (f"%{q}%", f"%{q}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 五类容器查询
    #
    # 阶段二把五类各自的产物物化了（item / pc_skill / npc / rule_entry+rule_flow / lore_topic）。
    # 运行期**优先查产物表**：DM 要的是"这张卡/这套步骤"，不是"最像的一段话"；
    # 产物表不存在（旧库）时才回落到通用实体层，保证老数据也能用。

    def _block_of(self, block_id: str, n: int = 900) -> dict | None:
        if not block_id:
            return None
        b = self.con.execute(
            "SELECT title,page,text,kind FROM block WHERE id=?", (block_id,)
        ).fetchone()
        return {"title": b["title"], "page": b["page"], "kind": b["kind"],
                "text": (b["text"] or "")[:n]} if b else None

    def item_card(self, name: str) -> dict:
        """物品：名字 → 简介 + 字段（DM 按名直读）。"""
        norm = re.sub(r"\s+", "", name or "")
        row = self.con.execute(
            "SELECT * FROM item WHERE name_norm=? OR name LIKE ? ORDER BY length(name) LIMIT 1",
            (norm, f"%{norm}%"),
        ).fetchone()
        if not row:
            return self.card(name, etype="item")
        fields = [dict(r) for r in self.con.execute(
            "SELECT key,value,quote FROM item_field WHERE item_id=?", (row["id"],))]
        return {"name": row["name"], "etype": "item", "page": row["page"],
                "summary": row["summary"], "fields": fields,
                "source": self._block_of(row["block_id"])}

    def skill_card(self, name: str) -> dict:
        """技能：名字 → 基础值 + 描述（玩家声明要用技能时直查）。"""
        norm = re.sub(r"\s+", "", name or "")
        row = self.con.execute(
            "SELECT * FROM pc_skill WHERE name=? OR name LIKE ? ORDER BY length(name) LIMIT 1",
            (name, f"%{name}%"),
        ).fetchone()
        if not row:
            return self.card(name, etype="skill")
        return {"name": row["name"], "etype": "skill", "page": row["page"],
                "base_value": row["base_value"], "description": row["description"],
                "source": self._block_of(row["block_id"])}

    def npc_card(self, name: str) -> dict:
        """NPC：名字 → 属性 + 能力（只读档案）。"""
        norm = re.sub(r"\s+", "", name or "")
        row = self.con.execute(
            "SELECT * FROM npc WHERE name=? OR name LIKE ? ORDER BY length(name) LIMIT 1",
            (name, f"%{norm}%"),
        ).fetchone()
        if not row:
            return self.card(name, etype="monster")
        fields = [dict(r) for r in self.con.execute(
            "SELECT key,value,dice,quote FROM npc_field WHERE npc_id=?", (row["id"],))]
        abilities = [dict(r) for r in self.con.execute(
            "SELECT name,text,quote FROM npc_ability WHERE npc_id=?", (row["id"],))]
        # pc_relation 是切分阶段产出的静态关系图。它和本局的 entity_state
        # 有意分开：这里返回的是剧本事实，运行时关系变化由事件/世界层记录。
        relations = []
        try:
            relations = [dict(r) for r in self.con.execute(
                "SELECT src,dst,rel,quote FROM pc_relation "
                "WHERE src=? OR dst=? ORDER BY id LIMIT 20",
                (row["name"], row["name"]),
            )]
        except Exception:  # 兼容尚未重编译、没有关系表的旧知识库
            relations = []
        return {"name": row["name"], "etype": row["etype"], "page": row["page"],
                "readonly": True, "fields": fields, "abilities": abilities,
                "relations": relations, "source": self._block_of(row["block_id"])}

    def rule_card(self, name: str) -> dict:
        """规则：名字 → 子类 + 判定步骤 + 流程图。"""
        row = self.con.execute(
            "SELECT * FROM rule_entry WHERE title LIKE ? ORDER BY length(title) LIMIT 1",
            (f"%{name}%",),
        ).fetchone()
        if not row:
            return self.card(name)
        subtype = None
        if row["subtype_id"]:
            s = self.con.execute("SELECT name FROM rule_subtype WHERE id=?",
                                 (row["subtype_id"],)).fetchone()
            subtype = s["name"] if s else None
        steps = [dict(r) for r in self.con.execute(
            "SELECT seq,kind,text,quote FROM rule_step WHERE rule_id=? ORDER BY seq", (row["id"],))]
        flow = self.con.execute("SELECT kind,nodes,edges FROM rule_flow WHERE rule_id=?",
                                (row["id"],)).fetchone()
        return {"name": row["title"], "subtype": subtype, "page": row["page"],
                "text": (row["text"] or "")[:900], "steps": steps,
                "flow": ({"kind": flow["kind"], "nodes": json.loads(flow["nodes"]),
                          "edges": json.loads(flow["edges"])} if flow else None)}

    def lore_topics(self, query: str, k: int = 5) -> list[dict]:
        """世界观：先找主题（摘要层），再把挂在这个主题下的原文一起返回。"""
        toks = [t for t in tokenize(query) if len(t) > 1][:6]
        rows: list = []
        if toks:
            like = " OR ".join(["label LIKE ? OR summary LIKE ?"] * len(toks))
            args: list = []
            for t in toks:
                args.extend([f"%{t}%", f"%{t}%"])
            rows = self.con.execute(
                f"SELECT * FROM lore_topic WHERE {like} ORDER BY members DESC LIMIT ?",
                (*args, k),
            ).fetchall()
        if not rows:  # 主题层没命中 → 回落到块级混合检索（仍然是原文）
            return [{"fallback": h.brief(240), "page": h.page, "title": h.title}
                    for h in self.search(query, k=k, kinds=["lore"])]
        out = []
        for r in rows:
            members = [dict(m) for m in self.con.execute(
                "SELECT block_id,similarity FROM lore_member WHERE topic_id=? ORDER BY similarity DESC",
                (r["id"],))]
            texts = []
            for m in members[:5]:
                b = self._block_of(m["block_id"], 400)
                if b:
                    texts.append({"page": b["page"], "text": b["text"], "sim": m["similarity"]})
            out.append({"topic": r["label"], "summary": r["summary"], "members": len(members),
                        "originals": texts})
        return out

    # ---------------------------------------------------------------- 给 DM 的工具契约

    TOOLS = [
        {
            "name": "kb_search",
            "desc": "语义+字面混合检索规则书原文（规则、世界观、剧情都在这）。返回原文片段与页码。",
            "args": {"query": "玩家动作/问题", "k": "返回条数，默认6", "kind": "可选：rule/pc/tool/npc/lore"},
        },
        {
            "name": "kb_item",
            "desc": "按名字精确查物品/武器/装备（返回该物品的全部字段与原文）。",
            "args": {"name": "物品名，如 燧发手枪"},
        },
        {
            "name": "kb_skill",
            "desc": "按名字精确查技能（基础值 + 描述 + 对抗技能）。玩家声明要用技能时用它。",
            "args": {"name": "技能名，如 考古学"},
        },
        {
            "name": "kb_npc",
            "desc": "按名字精确查 NPC/怪物卡（属性、护甲、特殊能力）。只读，不可修改。",
            "args": {"name": "名字，如 拜亚基"},
        },
        {
            "name": "kb_rule",
            "desc": "查规则条目；给了 name 就同时返回流程图（判定步骤与分支）。",
            "args": {"name": "规则名或主题，如 战斗 / 理智检定"},
        },
        {
            "name": "kb_state",
            "desc": "查实体在本次团里的运行态（活着/死了/敌对）。**状态以这里为准，不要相信检索到的旧文本**。",
            "args": {"campaign": "团 id", "entity": "实体名"},
        },
        {
            "name": "kb_progress",
            "desc": "查剧情推进到哪了、下一步有哪些可走的场景。",
            "args": {"campaign": "团 id"},
        },
    ]
