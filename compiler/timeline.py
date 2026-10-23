"""时间线 / 进度层：**和知识分类正交的第二根轴**。

五类分类回答"这块文本是什么"；时间线回答"故事走到哪了"。两者不能混：

- 分类是**静态**的（规则永远是规则），可以全量索引、随便检索；
- 进度是**动态**的（卡特已经死了、门已经开了），必须强一致，每轮全量注入，不能靠检索。

这也是为什么这里单独有一张 `entity_state`：实体在书里的定义是只读的（`entity.readonly=1`），
但它在**这一局**里的状态是可变的。玩家一枪打死 NPC，改的是 `entity_state`，不是书。

"时间 RAG"由两件事组成：
1. 块级的 `valid_from / valid_to`（世界时间区间）——检索时按当前世界时间过滤；
2. 事件级的 `timeline_event` —— 只追加，不修改，能回放。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from . import db

# 世界时间只认"XXXX 年"，且排除"XXXX 年版"（那是版权/版本年，不是故事里的时间）
YEAR_RE = __import__("re").compile(r"(?<![\d.])(1[89]\d{2}|20\d{2})\s*年(?!\s*版)")


def extract_year(text: str) -> int | None:
    """从文本里读世界时间："在1925 年的夏季" → 1925。

    取**出现次数最多的年份**而不是第一个：一段话里常有"1937 年洛夫克拉夫特去世……1925 年夏天"，
    第一个往往是传记年份，出现在正文中间的那个才是故事时间。读不到就返回 None（= 超时间）。
    """
    ys = [int(m.group(1)) for m in YEAR_RE.finditer(text or "")]
    ys = [y for y in ys if 1850 <= y <= 2050]
    if not ys:
        return None
    return Counter(ys).most_common(1)[0][0]


class Timeline:
    def __init__(self, con):
        self.con = con

    # ---- 团的建立 ----
    def ensure_campaign(self, book_id: str, name: str = "default") -> str:
        cid = f"{book_id}:{name}"
        row = self.con.execute("SELECT id FROM campaign WHERE id=?", (cid,)).fetchone()
        if row:
            return cid
        self.con.execute(
            "INSERT INTO campaign(id,book_id,name,world_time,cursor,created_at) VALUES(?,?,?,?,?,?)",
            (cid, book_id, name, "", db.j({"done": [], "flags": []}),
             datetime.now().isoformat(timespec="seconds")),
        )
        self.con.commit()
        return cid

    # ---- 场景（从"模组"章里取，不预设剧本格式）----
    def _scene_chapters(self, book_id: str) -> list[str]:
        """哪些一级章是"剧情章"。**只认一级章**：第十章里有个小节叫"10.21 创作模组"，
        按关键字模糊匹配会把整个主持游戏章都当剧情（实测把"创作模组"当成了第一个场景）。"""
        import re as _re

        rows = self.con.execute(
            "SELECT id,title FROM section WHERE book_id=? AND level=1 ORDER BY page_start", (book_id,)
        ).fetchall()
        return [
            r["id"]
            for r in rows
            if _re.search(r"(模组|剧本|冒险|战役|剧本集)", r["title"] or "")
        ]

    def scenes(self, book_id: str) -> list[dict]:
        chaps = self._scene_chapters(book_id)
        if not chaps:
            return []
        marks = ",".join("?" * len(chaps))
        rows = self.con.execute(
            f"""SELECT b.id,b.title,b.page,b.subtype,s.path,s.level FROM block b
                JOIN section s ON s.id=b.section_id
                WHERE b.book_id=? AND (b.section_id IN ({marks})
                       OR s.parent_id IN ({marks}))
                  AND b.kind IN ('lore','rule')
                  AND b.title <> ''
                  AND (b.meta NOT LIKE '%"is_table": true%')
                ORDER BY b.page, b.ord""",
            [book_id] + chaps + chaps,
        ).fetchall()
        # 去掉"整章开头那段导语"（标题等于章名的那种块）
        chap_titles = {
            r["title"] for r in self.con.execute(
                f"SELECT title FROM section WHERE id IN ({marks})", chaps
            ).fetchall()
        }
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            t = (r["title"] or "").strip()
            if not t or t in chap_titles:
                continue
            # 引文出处（"——H.P.洛夫克拉夫特，《…》"）不是场景；同名块只算一个场景
            if t.startswith("——") or "《" in t:
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(dict(r))
        return out

    def start(self, book_id: str, name: str = "default") -> dict:
        cid = self.ensure_campaign(book_id, name)
        scenes = self.scenes(book_id)
        if scenes:
            self.con.execute(
                "UPDATE campaign SET current_section=?, current_page=? WHERE id=?",
                (scenes[0]["id"], scenes[0]["page"], cid),
            )
            self.log(cid, "scene", "开场", scenes[0]["title"], block_id=scenes[0]["id"],
                     section_id=scenes[0]["id"], page=scenes[0]["page"], irreversible=0)
        return self.progress(cid)

    def advance(self, campaign_id: str, scene: str | None = None, note: str = "",
                world_time: str = "", irreversible: bool = False) -> dict:
        """推进到下一个场景 / 指定场景。只追加事件，不删历史。"""
        cur = self.con.execute("SELECT * FROM campaign WHERE id=?", (campaign_id,)).fetchone()
        if not cur:
            raise KeyError(campaign_id)
        book_id = cur["book_id"]
        scenes = self.scenes(book_id)
        target = None
        if scene:
            for s in scenes:
                if scene in (s["title"] or "") or scene == s["id"]:
                    target = s
                    break
        if target is None and scenes:
            ids = [s["id"] for s in scenes]
            idx = ids.index(cur["current_section"]) if cur["current_section"] in ids else -1
            target = scenes[min(idx + 1, len(scenes) - 1)]
        if target:
            self.con.execute(
                "UPDATE campaign SET current_section=?, current_page=?, world_time=? WHERE id=?",
                (target["id"], target["page"], world_time or cur["world_time"] or "", campaign_id),
            )
            self.con.commit()
            self.log(campaign_id, "scene", "推进", target["title"], block_id=target["id"],
                     page=target["page"], world_time=world_time, payload={"note": note},
                     irreversible=irreversible)
        return self.progress(campaign_id)

    def log(self, campaign_id: str, kind: str, title: str, body: str = "",
            block_id: str = "", section_id: str = "", page: int = 0,
            world_time: str = "", payload: dict | None = None, irreversible: bool = False) -> int:
        seq = self.con.execute(
            "SELECT COALESCE(MAX(seq),0) s FROM timeline_event WHERE campaign_id=?", (campaign_id,)
        ).fetchone()["s"] + 1
        self.con.execute(
            """INSERT INTO timeline_event(campaign_id,seq,kind,title,body,section_id,block_id,page,
               world_time,payload,irreversible,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (campaign_id, seq, kind, title, body, section_id, block_id, page, world_time,
             db.j(payload or {}), 1 if irreversible else 0,
             datetime.now().isoformat(timespec="seconds")),
        )
        self.con.commit()  # 时间线是"必须落盘"的一层：不 commit 就等于没发生
        return seq

    # ---- 状态真相源 ----
    def set_state(self, campaign_id: str, entity_id: str, status: str, **fields) -> None:
        seq = self.con.execute(
            "SELECT COALESCE(MAX(seq),0) s FROM timeline_event WHERE campaign_id=?", (campaign_id,)
        ).fetchone()["s"]
        old = self.con.execute(
            "SELECT fields FROM entity_state WHERE campaign_id=? AND entity_id=?",
            (campaign_id, entity_id),
        ).fetchone()
        merged = db.uj(old["fields"], {}) if old else {}
        merged.update(fields)
        self.con.execute(
            """INSERT INTO entity_state(campaign_id,entity_id,status,fields,updated_seq)
               VALUES(?,?,?,?,?)
               ON CONFLICT(campaign_id,entity_id) DO UPDATE SET
                 status=excluded.status, fields=excluded.fields, updated_seq=excluded.updated_seq""",
            (campaign_id, entity_id, status, db.j(merged), seq),
        )
        self.con.commit()
        name = self.con.execute("SELECT name FROM entity WHERE id=?", (entity_id,)).fetchone()
        self.log(campaign_id, "state", f"状态变更：{name['name'] if name else entity_id}", status,
                 payload={"entity_id": entity_id, "status": status, **fields}, irreversible=True)

    def get_state(self, campaign_id: str, entity: str) -> dict:
        row = self.con.execute(
            """SELECT es.*, e.name, e.etype FROM entity_state es
               JOIN entity e ON e.id=es.entity_id
               WHERE es.campaign_id=? AND (e.name=? OR e.name_norm=? OR es.entity_id=?)""",
            (campaign_id, entity, entity, entity),
        ).fetchone()
        if not row:
            return {"entity": entity, "status": "unknown",
                    "note": "本次团里没有状态记录 → 按书里的默认描述处理"}
        return {"entity": row["name"], "etype": row["etype"], "status": row["status"],
                "fields": db.uj(row["fields"], {}), "seq": row["updated_seq"]}

    # ---- 进度 ----
    def progress(self, campaign_id: str) -> dict:
        camp = self.con.execute("SELECT * FROM campaign WHERE id=?", (campaign_id,)).fetchone()
        if not camp:
            return {"error": "没有这个团"}
        scenes = self.scenes(camp["book_id"])
        cur = camp["current_section"]
        idx = next((i for i, s in enumerate(scenes) if s["id"] == cur), -1)
        done = {r["block_id"] for r in self.con.execute(
            "SELECT block_id FROM timeline_event WHERE campaign_id=? AND kind='scene'", (campaign_id,)
        ).fetchall()}
        nxt = [s for s in scenes[idx + 1 : idx + 4]]
        return {
            "campaign": campaign_id,
            "world_time": camp["world_time"] or "",
            "current": scenes[idx] if idx >= 0 else None,
            "next": nxt,
            "scene_total": len(scenes),
            "scene_done": len(done),
            "events": self.con.execute(
                "SELECT COUNT(*) c FROM timeline_event WHERE campaign_id=?", (campaign_id,)
            ).fetchone()["c"],
        }

    def replay(self, campaign_id: str, limit: int = 50) -> list[dict]:
        rows = self.con.execute(
            "SELECT seq,kind,title,body,page,world_time FROM timeline_event "
            "WHERE campaign_id=? ORDER BY seq LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
