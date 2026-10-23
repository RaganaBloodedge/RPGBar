"""世界层：一局一个独立库，记「世界现在是什么样」。

和 KB 的分工（这是硬边界）：

    kb.sqlite   只读事实层：规则、技能、物品、怪物、世界观 —— 可以重编译
    world/*.sqlite  世界层：这局的地点、在场的东西、线索、时间 —— 一局一份，绝不和 KB 混
    chronicle-*.jsonl  叙事流水：旁白/行动/骰点，只追加

为什么必须分开：KB 是可重新生成的产物，世界层是不可重来的存档；混在一起，
重编译一次就把存档毁了，也没法回滚、没法并发两局。

「玩家说开门但没有门」就靠这里解决：`present()` 给出**当前场景真实存在的东西**，
`declare()` 是 DM 把新出现的布景/人物登记进来的唯一入口。没登记的东西，下轮就没了。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from .config import USER_DIR

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS scene(
    id TEXT PRIMARY KEY, parent_id TEXT, place TEXT, label TEXT,
    description TEXT, source TEXT, created_turn INTEGER DEFAULT 0,
    visited INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS entity(
    id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT NOT NULL, name TEXT NOT NULL,
    kind TEXT NOT NULL, detail TEXT DEFAULT '', source TEXT DEFAULT 'dm',
    first_turn INTEGER DEFAULT 0, readonly INTEGER DEFAULT 0,
    UNIQUE(scene_id, name, kind));
CREATE TABLE IF NOT EXISTS entity_fact(
    entity_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, quote TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS thread(
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, status TEXT DEFAULT 'open',
    opened_turn INTEGER DEFAULT 0, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS pointer(k TEXT PRIMARY KEY, v TEXT);
"""

KINDS = ("object", "npc", "creature", "clue", "place", "door")
_KIND_CN = {"object": "物件", "npc": "人物", "creature": "生物", "clue": "线索",
            "place": "地点", "door": "门/通道"}


def _key(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


class World:
    """一局的世界状态。所有写操作都同步落盘（sqlite），重启不丢。"""

    def __init__(self, path: Path, script: str = "", kb: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), timeout=10)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SCHEMA)
        self.con.commit()
        self.script = script
        self.kb = kb
        if not self.get("script"):
            self.set("script", script)
            self.set("kb", kb)
            self.set("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))

    # ---------------------------------------------------------------- 路径
    @staticmethod
    def path_for(script_rel: str, room: str) -> Path:
        """世界库位置：data/worlds/<剧本名>/<房间号>.sqlite（每个剧本一个目录）。"""
        stem = Path(script_rel).stem if script_rel else "sandbox"
        stem = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fa5]+", "_", stem)[:40] or "sandbox"
        room = re.sub(r"[^0-9A-Za-z_\-]+", "", room or "")[:16] or "room"
        return USER_DIR / "data" / "worlds" / stem / f"{room}.sqlite"

    @classmethod
    def for_room(cls, script_rel: str, room: str, kb: str = "") -> "World":
        return cls(cls.path_for(script_rel, room), script=script_rel, kb=kb)

    def close(self) -> None:
        try:
            self.con.close()
        except sqlite3.Error:
            pass

    # ---------------------------------------------------------------- 键值
    def get(self, key: str, default: str = "") -> str:
        row = self.con.execute("SELECT v FROM pointer WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set(self, key: str, value: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO pointer(k,v) VALUES(?,?)", (key, str(value)))
        self.con.commit()

    def bump_turn(self) -> int:
        turn = int(self.get("turn", "0") or 0) + 1
        self.set("turn", turn)
        return turn

    # ---------------------------------------------------------------- 场景
    def scene(self, scene_id: str | None = None) -> dict | None:
        sid = scene_id or self.get("scene")
        row = self.con.execute("SELECT * FROM scene WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def add_scene(self, scene_id: str, label: str, place: str = "", description: str = "",
                  parent: str = "", source: str = "script") -> dict:
        self.con.execute(
            "INSERT OR IGNORE INTO scene(id,parent_id,place,label,description,source,created_turn,visited)"
            " VALUES(?,?,?,?,?,?,?,0)",
            (scene_id, parent or "", place, label, description, source,
             int(self.get("turn", "0") or 0)),
        )
        self.con.commit()
        return self.scene(scene_id) or {}

    def enter(self, scene_id: str) -> None:
        self.con.execute("UPDATE scene SET visited=1 WHERE id=?", (scene_id,))
        self.set("scene", scene_id)
        self.con.commit()

    # ---------------------------------------------------------------- 实体
    def declare(self, name: str, kind: str = "object", detail: str = "",
                scene_id: str | None = None) -> dict:
        """登记一个**新出现**的东西（布景/人物/线索）。重复登记返回原有的。"""
        kind = kind if kind in KINDS else "object"
        sid = scene_id or self.get("scene")
        name = (name or "").strip()[:40]
        if not name or not sid:
            return {}
        row = self.con.execute(
            "SELECT * FROM entity WHERE scene_id=? AND name=? AND kind=?", (sid, name, kind)
        ).fetchone()
        if row:
            if detail and not row["detail"]:
                self.con.execute("UPDATE entity SET detail=? WHERE id=?", (detail[:200], row["id"]))
                self.con.commit()
            return dict(row)
        self.con.execute(
            "INSERT INTO entity(scene_id,name,kind,detail,source,first_turn) VALUES(?,?,?,?,?,?)",
            (sid, name, kind, detail[:200], "dm", int(self.get("turn", "0") or 0)),
        )
        self.con.commit()
        return dict(self.con.execute(
            "SELECT * FROM entity WHERE scene_id=? AND name=? AND kind=?", (sid, name, kind)
        ).fetchone())

    def present(self, scene_id: str | None = None) -> list[dict]:
        sid = scene_id or self.get("scene")
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM entity WHERE scene_id=? ORDER BY id", (sid,))]

    def find(self, name: str) -> dict | None:
        """全库找（先当前场景，再别处）。用于回答"这东西在不在、在哪"。"""
        key = _key(name)
        if not key:
            return None
        sid = self.get("scene")
        for scope in (sid, None):
            sql = "SELECT * FROM entity WHERE scene_id=?" if scope else "SELECT * FROM entity"
            args = (scope,) if scope else ()
            for row in self.con.execute(sql + " ORDER BY id", args):
                if _key(row["name"]) == key or key in _key(row["name"]):
                    return dict(row)
        return None

    def facts(self, entity_id: int) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT key,value,quote,source FROM entity_fact WHERE entity_id=?", (entity_id,))]

    def add_fact(self, entity_id: int, key: str, value: str, quote: str = "",
                 source: str = "dm") -> None:
        self.con.execute(
            "INSERT INTO entity_fact(entity_id,key,value,quote,source) VALUES(?,?,?,?,?)",
            (entity_id, key[:40], str(value)[:120], quote[:200], source))
        self.con.commit()

    # ---------------------------------------------------------------- 线索
    def open_thread(self, title: str, note: str = "") -> int:
        cur = self.con.execute(
            "INSERT INTO thread(title,status,opened_turn,note) VALUES(?,?,?,?)",
            (title[:80], "open", int(self.get("turn", "0") or 0), note[:200]))
        self.con.commit()
        return int(cur.lastrowid)

    def threads(self, status: str = "open") -> list[dict]:
        sql = "SELECT * FROM thread" + (" WHERE status=?" if status else "")
        return [dict(r) for r in self.con.execute(sql + " ORDER BY id", (status,) if status else ())]

    # ---------------------------------------------------------------- 注入给 DM 的两段
    def present_text(self) -> str:
        """给 DM 的【现场】。**肯定式列举**：先写"原文描述的（真实存在）"，再写登记过的。

        早先写成"这里没有登记过任何东西"会被 4B 读成"什么都没有"，连原文写着的灯都否认。
        """
        sc = self.scene()
        if not sc:
            return ""
        desc = re.sub(r"\s+", " ", sc.get("description") or "")
        lines = [f"地点：{sc['place'] or sc['label']}（{sc['id']}）"]
        if desc:
            lines.append("【原文描述的现场】（这些真实存在）")
            lines.append(desc[:300])
        items = self.present()
        if items:
            lines.append("【你另外登记过的东西】")
            for e in items[:12]:
                note = f"（{e['detail']}）" if e["detail"] else ""
                lines.append(f"- {_KIND_CN.get(e['kind'], e['kind'])}「{e['name']}」{note}"
                             + ("〔不可改，来自设定〕" if e["readonly"] else ""))
        return "\n".join(lines)

    def digest_text(self, turns: int = 6) -> str:
        sc = self.scene()
        parts = [
            f"当前地点：{sc['place'] or sc['label']}（第 {self.get('turn', '0')} 轮"
            f"{'，第 ' + self.get('day', '1') + ' 天' if self.get('day') else ''}）",
        ]
        todo = self.threads("open")
        if todo:
            parts.append("还没收束的线索：" + "；".join(t["title"] for t in todo[:6]))
        visited = [dict(r) for r in self.con.execute(
            "SELECT id,label,place FROM scene WHERE visited=1 ORDER BY id")]
        if len(visited) > 1:
            parts.append("已经去过/知道的地点：" + "、".join(
                (s["place"] or s["label"] or s["id"]) for s in visited[:8]))
        return "\n".join(parts)

    # ---------------------------------------------------------------- 时间
    def advance(self, minutes: int) -> dict | None:
        """推进游戏内时间。返回跨昼夜事件（没有就是 None）。"""
        try:
            minutes = max(1, min(int(minutes), 240))
        except (TypeError, ValueError):
            minutes = 10
        clock = int(self.get("minutes", "360") or 360) + minutes
        day = int(self.get("day", "1") or 1)
        event = None
        while clock >= 1440:
            clock -= 1440
            day += 1
            event = {"type": "time", "text": f"第 {day} 天开始了。"}
        phase = "day" if 6 * 60 <= clock < 18 * 60 else "night"
        old = self.get("phase", "day")
        self.set("minutes", clock)
        self.set("day", day)
        self.set("phase", phase)
        if event is None and old != phase:
            event = {"type": "time",
                     "text": ("天黑了。" if phase == "night" else "天亮了。")}
        return event

    # ---------------------------------------------------------------- 指代校验
    _PRONOUN = {"这里", "那里", "你们", "我们", "他们", "自己", "什么", "东西", "地方",
                "时候", "现在", "刚才", "面前", "周围", "四周", "附近", "身上", "手里"}

    def scene_text(self) -> str:
        sc = self.scene() or {}
        return re.sub(r"\s+", "", (sc.get("description") or ""))

    def unresolved(self, text: str, kb_names: list[str] | None = None) -> list[str]:
        """玩家话里**提到但现场/原文/库里都没有**的名词。

        为什么放在服务端而不是让模型判断：4B 分不清"玩家提到了"和"这里存在"。
        实测同一句话，它要么顺着演（灯座上刻着符号），要么一律否认（这里没有青铜灯）。
        这里用 jieba 只做**名词抽取 + 存在性比对**（机械活），判断留给 DM。
        """
        try:
            import jieba.posseg as pseg
        except Exception:  # noqa: BLE001
            return []
        here = {_key(e["name"]) for e in self.present()}
        scene = self.scene_text()
        kb = {_key(n) for n in (kb_names or [])}
        out: list[str] = []
        for w, flag in pseg.cut(text or ""):
            if not flag.startswith("n"):
                continue
            if len(w) < 2 or w in self._PRONOUN:
                continue
            if w[0] in "这那哪几每各某":
                continue  # "那盏/这个/某个" 是量词短语，不是指代对象
            k = _key(w)
            if k in here or k in kb:
                continue
            if k and k in scene:
                continue
            if any(k and k in _key(o) for o in out):
                continue
            out.append(w)
            if len(out) >= 4:
                break
        return out
