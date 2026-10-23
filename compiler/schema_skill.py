"""★ 字段自动拓展 Skill：**表结构由 Agent 读到什么就长什么**。

这是整个编译器的"自适应"部分。它干的活只有一句话：**见到一个没见过的字段，就把它登记
成字段字典里的一条，并且带上出生证明；见到一个像已知字段的新写法，就并成它的别名。**

三条硬规矩（都是踩过坑才定的）：

1. **无证据不立字段**。每个新字段必须存 `evidence_quote`（原文那一句）和页码。
   没有原文的字段等于模型编的，宁可不要。
2. **同义合并优先于新增**。"价格"/"花费"/"售价"/"价格($)"是同一列，不能长成四个字段，
   否则角色卡上会出现三行"价格"。合并只改 `aliases`，不动已写入的 fact。
3. **类型靠值推，不靠标签猜**。看到 "1D6+1" 才知道是骰子，看到 "95" 才知道是数字，
   看到 "稀/CP/C" 才知道是枚举。标签叫"伤害"但值是"见 6.6"的，就是 ref 而不是 dice。

由此带来的性质：同一本书换一版（多一个"灵力"属性），不需要改代码、不需要迁移数据库，
重新跑一遍编译，字段字典自己会多一条 —— 这是"开放模式"真正的意义。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field as dc_field
from datetime import datetime

from . import db

# ------------------------------------------------------------------ 归一化

_FULL2HALF = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ（）％＄"}

# 同义写法：左边是"标准字段"，右边是见过的其它写法。
# 这张表**不是**预设字段清单，只是"合并规则"——没在这里出现的字段照样能自动长出来。
SYNONYMS: dict[str, list[str]] = {
    "价格": ["花费", "售价", "费用", "价格($)", "成本", "价值"],
    "伤害": ["损伤", "伤害值", "伤害骰"],
    "护甲": ["装甲", "护甲值", "盔甲"],
    "消耗": ["耗费", "消耗量", "施法消耗"],
    "射程": ["基础射程", "有效射程", "射程范围"],
    "移动": ["移动力", "移动速度"],
    "耐久": ["耐久度", "HP", "生命值", "体力值"],
    "理智": ["SAN", "理智值", "SAN值"],
    "魔法值": ["MP", "魔力", "魔法点"],
    "伤害加成": ["平均伤害加成", "伤害奖励", "DB"],
    "体格": ["平均体格", "Build", "体格值"],
    "技能": ["技能值", "技能等级"],
}
_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _alts in SYNONYMS.items():
    for _a in _alts:
        _ALIAS_TO_CANON[_a] = _canon

_UNIT_RE = re.compile(r"[（(]\s*([^）)]{1,8})\s*[）)]")
# 骰子表达式：5D6 / 1D6+1 / 3D6+3×5 / 2D6+12×5（含"×5"的倍数写法，规则书里极常见）
_DICE_RE = re.compile(r"^\d*[dD]\d+(\s*([+\-×xX*]\s*\d+))*$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
_RANGE_RE = re.compile(r"^\d+\s*[/~\-]\s*\d+$")
_REF_RE = re.compile(r"^(见|参考|参见|RB\d|第.{1,6}页)")
# 货币：$17.95 / ￥9.95-35.00 / 69 美分 / 25 美分/ 磅
_MONEY_RE = re.compile(r"^[$￥]\s*[\d.,]+|^[\d.,]+\s*(美分|美元|便士|先令|英镑)")


def normalize_label(raw: str) -> tuple[str, str]:
    """'价格($)' → ('价格', '$')；'平均伤害加成' → ('伤害加成', '')。返回 (标签, 单位)。"""
    s = (raw or "").translate(_FULL2HALF).strip()
    s = re.sub(r"\s+", "", s)
    s = s.strip("：:·、，,。")
    unit = ""
    m = _UNIT_RE.search(s)
    if m:
        unit = m.group(1)
        s = _UNIT_RE.sub("", s).strip()
    if s.upper() in ("HP", "MP", "SAN", "DB"):
        return s.upper(), unit
    canon = _ALIAS_TO_CANON.get(s) or _ALIAS_TO_CANON.get(s.replace("值", ""))
    return (canon or s), unit


def infer_kind(value: str, unit: str = "") -> str:
    v = (value or "").strip()
    if not v:
        return "text"
    # 骰子常被括号包着："（5D6×5）" / "(1D6)"，先剥掉括号再判，否则会被认成 text
    v = v.strip("（）()【】[]").strip()
    if _DICE_RE.match(v):
        return "dice"
    if _REF_RE.match(v):
        return "ref"
    if v.endswith(("%", "％")):
        return "percent"
    if unit in ("$", "美元", "元") or _MONEY_RE.match(v):
        return "money"
    if _RANGE_RE.match(v):
        return "range"
    if _NUM_RE.match(v):
        return "num"
    if "、" in v or "，" in v:
        return "list"
    if len(v) <= 4 and not re.search(r"\d", v):
        return "enum"
    return "text"


def to_number(value: str) -> float | None:
    v = (value or "").strip().replace("％", "%")
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*%?$", v)
    return float(m.group(1)) if m else None


# ------------------------------------------------------------------ 字段定义

@dataclass
class FieldDef:
    key: str
    scope: str
    label: str
    kind: str
    unit: str = ""
    domain: str = ""
    cardinality: str = "one"
    aliases: list[str] = dc_field(default_factory=list)
    origin: str = ""
    first_page: int | None = None
    evidence_block: str = ""
    evidence_quote: str = ""
    confidence: float = 0.0
    status: str = "proposed"

    def to_row(self) -> tuple:
        return (
            self.key,
            self.scope,
            self.label,
            self.kind,
            self.unit,
            self.domain,
            self.cardinality,
            db.j(self.aliases),
            self.origin,
            self.first_page,
            self.evidence_block,
            self.evidence_quote,
            self.confidence,
            self.status,
            datetime.now().isoformat(timespec="seconds"),
        )


_ORIGIN_CONF = {"table_header": 0.95, "label_line": 0.85, "inline": 0.72, "llm": 0.6}


class SchemaSkill:
    """字段字典的读写入口。全流程只有一个实例，所有抽取器都通过它落字段。"""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self._cache: dict[str, FieldDef] = {}
        self._seq = con.execute("SELECT COALESCE(MAX(seq),0) s FROM schema_log").fetchone()["s"]
        self.new_fields = 0
        self.merged_aliases = 0

    # ---- 读 ----
    def get(self, key: str) -> FieldDef | None:
        if key in self._cache:
            return self._cache[key]
        row = self.con.execute("SELECT * FROM field WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        fd = FieldDef(
            key=row["key"], scope=row["scope"], label=row["label"], kind=row["kind"],
            unit=row["unit"] or "", domain=row["domain"] or "",
            cardinality=row["cardinality"] or "one", aliases=db.uj(row["aliases"], []),
            origin=row["origin"] or "", first_page=row["first_page"],
            evidence_block=row["evidence_block"] or "", evidence_quote=row["evidence_quote"] or "",
            confidence=row["confidence"] or 0.0, status=row["status"] or "proposed",
        )
        self._cache[key] = fd
        return fd

    def by_scope(self, scope: str) -> list[FieldDef]:
        rows = self.con.execute(
            "SELECT key FROM field WHERE scope=? ORDER BY rowid", (scope,)
        ).fetchall()
        return [self.get(r["key"]) for r in rows]

    # ---- 写 ----
    def observe(
        self,
        scope: str,
        label: str,
        value: str = "",
        *,
        origin: str = "label_line",
        page: int | None = None,
        block_id: str = "",
        quote: str = "",
    ) -> FieldDef:
        """看到 (标签, 值) 就调这里。返回最终落到哪个字段（可能是已有的，也可能是新建的）。"""
        norm, unit = normalize_label(label)
        if not norm:
            return None  # 空标签不值得建字段
        key = self._match_existing(scope, norm)

        if key:
            fd = self.get(key)
            # 见过的写法补进别名，方便以后召回
            if label.strip() and label.strip() != fd.label and label.strip() not in fd.aliases:
                fd.aliases.append(label.strip())
                self.con.execute(
                    "UPDATE field SET aliases=? WHERE key=?", (db.j(fd.aliases), fd.key)
                )
                self.merged_aliases += 1
                self._log("alias", fd.key, f"+{label.strip()}", page, block_id)
            # 值的类型如果比原来更"具体"，顺手修正（先见过"见6.6"记成 text，后来才有 1D6）
            guessed = infer_kind(value, unit)
            if value and fd.kind in ("text", "enum") and guessed in ("dice", "num", "money", "percent"):
                fd.kind = guessed
                self.con.execute("UPDATE field SET kind=? WHERE key=?", (guessed, fd.key))
                self._log("kind_fix", fd.key, f"text→{guessed}", page, block_id)
            return fd

        fd = FieldDef(
            key=f"{scope}.{norm}",
            scope=scope,
            label=norm,
            kind=infer_kind(value, unit),
            unit=unit,
            origin=origin,
            first_page=page,
            evidence_block=block_id,
            evidence_quote=(quote or value)[:200],
            confidence=_ORIGIN_CONF.get(origin, 0.6),
            status="accepted" if origin in ("table_header", "label_line") else "proposed",
        )
        self.con.execute(
            """INSERT OR IGNORE INTO field
               (key,scope,label,kind,unit,domain,cardinality,aliases,origin,first_page,
                evidence_block,evidence_quote,confidence,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            fd.to_row(),
        )
        self._cache[fd.key] = fd
        self.new_fields += 1
        self._log("new", fd.key, f"{fd.kind} ← {fd.evidence_quote[:60]}", page, block_id)
        return fd

    def observe_headers(
        self, scope: str, headers: list[str], values: list[str] | None = None,
        *, page: int | None = None, block_id: str = "",
    ) -> list[FieldDef]:
        """表头就是最权威的字段声明：一张 8 列表 → 一次长 8 个字段。"""
        out = []
        for i, h in enumerate(headers):
            v = values[i] if values and i < len(values) else ""
            out.append(
                self.observe(
                    scope, h, v, origin="table_header", page=page, block_id=block_id,
                    quote=(f"{' | '.join(headers)}" if not values else f"{h}={v}"),
                )
            )
        return out

    def _match_existing(self, scope: str, norm: str) -> str | None:
        """同义合并：先精确，再包含关系（"伤害" ⊂ "平均伤害"）。"""
        exact = f"{scope}.{norm}"
        if self.get(exact):
            return exact
        # 包含关系只在长度接近时成立（避免"伤害"把"伤害加成"吃掉 —— 它们其实是两个字段）
        for r in self.con.execute(
            "SELECT key,label FROM field WHERE scope=?", (scope,)
        ).fetchall():
            other = r["label"]
            if other == norm:
                return r["key"]
            if abs(len(other) - len(norm)) <= 2 and (other in norm or norm in other):
                return r["key"]
        return None

    def register_etype(self, etype: str, scope: str, page: int | None, evidence: str) -> bool:
        """实体类型也能长出来（"神话典籍"、"法术"、"职业"都不是写死的枚举）。"""
        known = db.uj(db.get_meta(self.con, "etypes", "[]"), []) or []
        if etype in known:
            return False
        known.append(etype)
        db.set_meta(self.con, "etypes", known)
        self._log("new_etype", etype, evidence[:80], page, "")
        return True

    def _log(self, action: str, key: str, detail: str, page, block_id) -> None:
        self._seq += 1
        self.con.execute(
            "INSERT INTO schema_log(seq,action,field_key,detail,page,block_id,at) VALUES(?,?,?,?,?,?,?)",
            (self._seq, action, key, detail, page, block_id, datetime.now().isoformat(timespec="seconds")),
        )

    # ---- 卡片：渲染时不预设列，按"字段从哪来"分组 ----
    def card(self, entity_id: str) -> dict:
        e = self.con.execute("SELECT * FROM entity WHERE id=?", (entity_id,)).fetchone()
        if not e:
            return {}
        rows = self.con.execute(
            """SELECT f.*, d.label, d.kind, d.unit, d.origin, d.scope
               FROM fact f JOIN field d ON d.key=f.field_key
               WHERE f.entity_id=? ORDER BY d.rowid""",
            (entity_id,),
        ).fetchall()
        groups = {"table_header": [], "label_line": [], "inline": [], "llm": []}
        seen: set[tuple[str, str]] = set()
        for r in rows:
            # 同一条事实可能在书里出现两次（技能索引页 + 技能列表页），卡片上只显示一次，
            # 但两条 fact 都要留着 —— 它们的页码/原文不同，都是证据
            key = (r["label"], str(r["raw"]))
            if key in seen:
                continue
            seen.add(key)
            item = {
                "label": r["label"],
                "value": r["raw"],
                "kind": r["kind"],
                "unit": r["unit"] or "",
                "page": r["page"],
                "quote": r["quote"],
            }
            groups.setdefault(r["origin"] or "inline", []).append(item)
        return {
            "id": e["id"],
            "name": e["name"],
            "etype": e["etype"],
            "subtype": e["subtype"] if "subtype" in e.keys() else "",
            "page": e["page"],
            "aliases": db.uj(e["aliases"], []),
            "summary": e["summary"] or "",
            # 三组不是预设的字段分类，而是"结构化程度"：表里的 > 标签行 > 正文内嵌
            "structured": groups["table_header"],
            "labelled": groups["label_line"],
            "inline": groups["inline"] + groups["llm"],
            "field_count": len(rows),
        }

    def audit(self, limit: int = 20) -> list[dict]:
        return [
            dict(r)
            for r in self.con.execute(
                "SELECT * FROM schema_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    # —— 全局类型修正 ——
    #
    # 字段常常是"先见表头、后见数据"：表头那一行没有值，`infer_kind("")` 只能给 text，
    # 于是 `item.伤害` 一开始是 text，等到 600 多个值都写进 fact 了，kind 还停在 text。
    # 这一步在编译末尾统一校正：**拿真实值的分布去投票**，谁占多数就改成谁。
    _SPECIFICITY = ["dice", "range", "percent", "money", "num", "enum", "ref", "list", "text"]

    def refine_kinds(self) -> int:
        fixed = 0
        rows = self.con.execute(
            """SELECT f.field_key, GROUP_CONCAT(f.raw, '¶') v, d.kind, d.unit
               FROM fact f JOIN field d ON d.key=f.field_key GROUP BY f.field_key"""
        ).fetchall()
        for r in rows:
            vals = [x for x in (r["v"] or "").split("¶") if x.strip()]
            if len(vals) < 2:
                continue
            counts: dict[str, int] = {}
            for v in vals:
                k = infer_kind(v, r["unit"] or "")
                counts[k] = counts.get(k, 0) + 1
            total = sum(counts.values())
            # 取"够多数（≥60%）且比现有更具体"的那个
            best = max(counts.items(), key=lambda kv: kv[1])
            if best[1] / total < 0.6 or best[0] == r["kind"]:
                continue
            if self._SPECIFICITY.index(best[0]) > self._SPECIFICITY.index(r["kind"] or "text"):
                continue
            self.con.execute("UPDATE field SET kind=? WHERE key=?", (best[0], r["field_key"]))
            self._log("kind_fix", r["field_key"], f"{r['kind']}→{best[0]}（{best[1]}/{total} 个值）", None, "")
            fixed += 1
        return fixed
