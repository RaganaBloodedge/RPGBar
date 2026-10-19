"""剧本读取 → 自动切片 → 生成「次级 prompt」的单一入口。

为什么要把这一层单独拆出来：

- 两个 Agent 的 **System prompt 只负责「我是谁、我的职责边界」**，是稳定不变的；
- 剧本相关的全部内容（世界观、语气、场景索引）属于**次级 prompt**，
  排在 System prompt 之后，由本模块从剧本文件自动生成，随剧本切换而变。

这样「职责」与「内容」解耦：换剧本只换次级 prompt，Agent 的人格与护栏一行都不用动。

支持两种输入：

1. **结构化剧本 JSON**：`{title, system, start_scene, flags, scenes[]}`，
   每个 scene 天然是一个切片；可驱动状态机（正常跑团）。
2. **无结构文本**（`.md` / `.txt`）：按 Markdown 标题切段；没有标题就按空行分段，
   再把段落装进不超过 `MAX_CHUNK_CHARS` 的片子里。无结构文本无法表达出口/检定，
   因此会**按切片顺序合成一条线性场景链**，让游戏仍能启动与推进（仅作试玩）。

切片（Chunk）既是次级 prompt 里的「索引」，也是 RAG 检索的单位。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_CHUNK_CHARS = 900  # 无结构文本单片上限
# 次级 prompt 里最多列多少个切片。超出的折叠成一行提示：这些是**每轮都要重发**的
# 固定开销，剧本一大（几十上百个场景）就会把本地 4B 的上下文吃光。
MAX_INDEX_CHUNKS = 30
_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*)$")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])")

# ---- 剧本声明的「人数」（可选段，供大厅建房时定房间容量）----
# 不写这个段不是笔误：缺省按 1–4 人（正好对应界面上的战士/法师/游侠/盗贼四个职业），
# 且**不产生告警**。写了但写坏（类型不对 / 区间反了 / 超过硬上限）才告警。
DEFAULT_PLAYERS = {"min": 1, "max": 4, "recommended": 4}
MAX_PLAYERS_LIMIT = 12  # 硬上限：房间容量最多这么多人（再多人一起跑一轮没有意义，也压不住显存）


@dataclass
class Chunk:
    """一个切片：剧本被切分后的最小单元。"""

    id: str
    title: str
    text: str = ""
    kind: str = "scene"  # scene（结构化场景）/ meta（剧本设定）/ section（无结构文本段落）
    keywords: list = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text or "")

    def preview(self, n: int = 120) -> str:
        t = re.sub(r"\s+", " ", self.text or "").strip()
        return t[:n] + ("…" if len(t) > n else "")

    def to_dict(self, n: int = 120) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "keywords": list(self.keywords or []),
            "chars": self.chars,
            "preview": self.preview(n),
        }


@dataclass
class ScriptDoc:
    """一份读进来的剧本（已切片）。"""

    title: str
    system: str = ""
    premise: str = ""
    start_scene: str = ""
    fmt: str = "json"  # json / text
    source: str = ""
    data: dict = field(default_factory=dict)
    chunks: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    players: dict = field(default_factory=lambda: dict(DEFAULT_PLAYERS))

    @property
    def structured(self) -> bool:
        return self.fmt == "json"

    @property
    def scenes(self) -> list:
        return self.data.get("scenes") or []

    def scene_chunks(self) -> list:
        return [c for c in self.chunks if c.kind == "scene"]

    def index_chunks(self) -> list:
        """次级 prompt 里要列出的切片（排除剧本设定本身）。"""
        return [c for c in self.chunks if c.kind != "meta"]

    def to_dict(self, preview: int = 120) -> dict:
        return {
            "title": self.title,
            "fmt": self.fmt,
            "structured": self.structured,
            "source": self.source,
            "start_scene": self.start_scene,
            "scenes": len(self.scenes),
            "flags": len(self.data.get("flags") or {}),
            "players": dict(self.players),
            "chunks": [c.to_dict(preview) for c in self.chunks],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------- 读取文件


def read_script_file(path) -> ScriptDoc:
    """读取一个剧本文件并切片。"""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    return parse_script(raw, source=str(path), name=p.stem)


# ---------------------------------------------------------------- 分发


def parse_script(raw: str, source: str = "", name: str = "") -> ScriptDoc:
    """把文件内容解析成已切片的 ScriptDoc（自动判定结构）。"""
    data = _try_json(raw)
    if isinstance(data, dict):
        return _from_structured(data, source=source, name=name)
    return _from_text(raw, source=source, name=name)


def _try_json(raw: str):
    raw = (raw or "").lstrip("\ufeff").strip()
    if not raw or raw[0] not in "{[":
        return None
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------- 结构化剧本


def _as_int(v):
    """把 JSON 里的数字宽容地取成 int（字符串 "4" 也认；布尔不是数字）。"""
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


def normalize_players(raw):
    """剧本里可选的 `players` 段 → (规范化 dict, 告警列表)。

    两种写法都收：

        "players": {"min": 2, "max": 5, "recommended": 4}
        "players": 4                      # 简写：只声明最大人数

    一律补齐成 `{"min", "max", "recommended"}`。**没写（None）= 用缺省值且不告警**
    —— 人数是可选段，不写不是笔误；写了但写坏（类型不对 / 区间反了 / 超硬上限）
    才告警，并把不能用的部分换回缺省，保证后面一定能算出容量。
    """
    base = dict(DEFAULT_PLAYERS)
    if raw is None or raw == "":
        return base, []

    warn = []

    def clamp(name, val, lo, hi):
        if val is None:
            return None
        if val < lo or val > hi:
            warn.append(f"players.{name}={val} 超出范围 {lo}–{hi}，已按 {max(lo, min(hi, val))} 处理")
            return max(lo, min(hi, val))
        return val

    if isinstance(raw, (int, float, str)) and not isinstance(raw, bool):
        n = _as_int(raw)
        if n is None:
            warn.append(f"players 写法无法识别（{raw!r}），已按缺省 {base['max']} 人处理")
            return base, warn
        n = clamp("max", n, 1, MAX_PLAYERS_LIMIT)
        warn.append(f"players 简写 {raw!r} 已按「最多 {n} 人」处理")
        return {"min": 1, "max": n, "recommended": n}, warn

    if not isinstance(raw, dict):
        warn.append(f"players 应是对象或整数，收到 {type(raw).__name__}，已按缺省处理")
        return base, warn

    lo = clamp("min", _as_int(raw.get("min")), 1, MAX_PLAYERS_LIMIT)
    hi = clamp("max", _as_int(raw.get("max")), 1, MAX_PLAYERS_LIMIT)
    rec = clamp("recommended", _as_int(raw.get("recommended")), 1, MAX_PLAYERS_LIMIT)
    lo = base["min"] if lo is None else lo
    hi = base["max"] if hi is None else hi
    if lo > hi:
        warn.append(f"players.min={lo} 大于 players.max={hi}，已对调")
        lo, hi = hi, lo
    rec = hi if rec is None else rec
    if rec < lo or rec > hi:
        warn.append(f"players.recommended={rec} 不在 {lo}–{hi} 区间内，已夹到区间内")
        rec = max(lo, min(hi, rec))
    return {"min": lo, "max": hi, "recommended": rec}, warn


def normalize_scene(s: dict, i: int = 0) -> dict:
    """补齐场景缺省字段 —— 让用户手写的剧本也能安全驱动状态机。"""
    out = dict(s or {})
    sid = str(out.get("id") or f"scene{i + 1}")
    out["id"] = sid
    out.setdefault("title", sid)
    out.setdefault("location", out.get("title") or "")
    out.setdefault("public_text", "")
    out.setdefault("dm_notes", "")
    out.setdefault("npcs", [])
    out.setdefault("keywords", [])
    out.setdefault("flags_required", [])
    out.setdefault("flags_set", [])
    out["checks"] = [_normalize_check(c) for c in (out.get("checks") or [])]
    out["exits"] = [_normalize_exit(e) for e in (out.get("exits") or [])]
    return out


def _normalize_check(c: dict) -> dict:
    out = dict(c or {})
    out.setdefault("id", "")
    out.setdefault("skill", "通用")
    out.setdefault("dc", 10)
    out.setdefault("keywords", [])
    out.setdefault("success", "")
    out.setdefault("fail", "")
    return out


def _normalize_exit(e: dict) -> dict:
    out = dict(e or {})
    out.setdefault("to", "")
    out.setdefault("label", out.get("to") or "")
    out.setdefault("keywords", [])
    out.setdefault("condition", None)
    return out


def _from_structured(data: dict, source: str = "", name: str = "") -> ScriptDoc:
    scenes = [normalize_scene(s, i) for i, s in enumerate(data.get("scenes") or [])]
    data = dict(data)
    data["scenes"] = scenes
    # 人数段在构造前就规范化好并写回 data —— 下游（ScriptStore）与切片看到的是同一份
    players, player_warnings = normalize_players(data.get("players"))
    data["players"] = players

    doc = ScriptDoc(
        title=str(data.get("title") or name or "未命名剧本"),
        system=str(data.get("system") or "").strip(),
        premise=str(data.get("premise") or "").strip(),
        start_scene=str(data.get("start_scene") or ""),
        fmt="json",
        source=source,
        data=data,
        chunks=_slice_structured(data),
        players=players,
    )
    doc.warnings.extend(player_warnings)

    ids = [s["id"] for s in scenes]
    if doc.start_scene not in ids:
        if ids:
            doc.warnings.append(
                f"start_scene「{doc.start_scene or '（空）'}」不存在，已回退到第一个场景 {ids[0]}"
            )
            doc.start_scene = ids[0]
        else:
            doc.warnings.append("剧本没有任何场景，无法开局")

    # 出口断链自检：指向不存在的场景属于剧本笔误，早点报出来
    dangling = sorted(
        {e["to"] for s in scenes for e in s["exits"] if e["to"] and e["to"] not in ids}
    )
    if dangling:
        doc.warnings.append("存在指向未知场景的出口：" + "、".join(dangling))
    unreachable = sorted(set(ids) - {doc.start_scene} - {e["to"] for s in scenes for e in s["exits"]})
    if unreachable:
        doc.warnings.append("无法从开局到达的场景：" + "、".join(unreachable))
    if not doc.system:
        doc.warnings.append("剧本未提供 system 段（世界观/语气），次级 prompt 会缺少基调说明")
    return doc


def _slice_structured(data: dict) -> list:
    """结构化剧本：一个 scene = 一个切片，另加一个「剧本设定」切片。"""
    chunks = [
        Chunk(
            "__meta__",
            str(data.get("title") or "剧本设定"),
            str(data.get("system") or "").strip(),
            kind="meta",
        )
    ]
    for s in data.get("scenes") or []:
        body = [str(s.get("public_text") or "")]
        if s.get("dm_notes"):
            body.append("【DM 内幕】" + str(s["dm_notes"]))
        chunks.append(
            Chunk(
                str(s["id"]),
                str(s.get("title") or s["id"]),
                "\n".join(b for b in body if b),
                kind="scene",
                keywords=list(s.get("keywords") or []),
            )
        )
    return chunks


# ---------------------------------------------------------------- 无结构文本


def _split_sections(text: str) -> list:
    """按 Markdown 标题切段；没有标题就整体一段。返回 [(标题, 正文)]。"""
    lines = text.split("\n")
    marks = []
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m:
            marks.append((i, m.group(2).strip()))
    if not marks:
        return [("", text.strip())]
    sections = []
    if marks[0][0] > 0:
        head = "\n".join(lines[: marks[0][0]]).strip()
        if head:
            sections.append(("前言", head))
    for idx, (ln_no, title) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        sections.append((title, "\n".join(lines[ln_no + 1 : end]).strip()))
    return sections


def _pack(body: str, max_chars: int = MAX_CHUNK_CHARS) -> list:
    """把段落贪心装箱成不超过 max_chars 的若干片段；超长段落按句号再切。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    flat = []
    for p in paras:
        if len(p) <= max_chars * 2:
            flat.append(p)
        else:
            flat.extend(s for s in _SENT_SPLIT_RE.split(p) if s.strip())
    out, buf = [], ""
    for p in flat:
        if buf and len(buf) + len(p) + 2 > max_chars:
            out.append(buf)
            buf = p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        out.append(buf)
    return out


def _slice_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    chunks, n = [], 0
    for title, body in _split_sections(text):
        pieces = _pack(body, max_chars) if body else []
        if not pieces:
            n += 1
            chunks.append(Chunk(f"c{n:02d}", title or f"片段{n}", "", kind="section"))
            continue
        for j, piece in enumerate(pieces):
            n += 1
            label = title or f"片段{n}"
            if len(pieces) > 1:
                label = f"{label}（{j + 1}/{len(pieces)}）"
            chunks.append(Chunk(f"c{n:02d}", label, piece, kind="section"))
    return chunks


def _from_text(raw: str, source: str = "", name: str = "") -> ScriptDoc:
    chunks = _slice_text(raw)
    title = name or (chunks[0].title if chunks else "") or "未命名剧本"
    doc = ScriptDoc(
        title=title,
        system="",
        premise="",
        start_scene="",
        fmt="text",
        source=source,
        data={"title": title, "system": "", "start_scene": "", "flags": {}, "scenes": [],
              "players": dict(DEFAULT_PLAYERS)},
        chunks=chunks,
        players=dict(DEFAULT_PLAYERS),
        warnings=[
            "无结构文本：切片用于次级 prompt 与 RAG 检索；已按切片顺序自动合成线性场景链（无检定、无线索），仅供试玩。"
        ],
    )
    scenes = _sandbox_scenes(doc)
    doc.data["scenes"] = scenes
    doc.start_scene = scenes[0]["id"] if scenes else ""
    if not chunks:
        doc.warnings.append("剧本文件没有可切分的内容")
    return doc


def _sandbox_scenes(doc: ScriptDoc) -> list:
    """无结构文本 → 线性场景链：走到第 i 片就能去第 i+1 片。"""
    body = [c for c in doc.chunks if c.kind == "section"]
    scenes = []
    for i, c in enumerate(body):
        nxt = body[i + 1] if i + 1 < len(body) else None
        exits = []
        if nxt:
            exits.append(
                {
                    "to": nxt.id,
                    "label": f"继续前往：{nxt.title}",
                    "keywords": ["继续", "前进", "往下", nxt.title],
                    "condition": None,
                }
            )
        scenes.append(
            {
                "id": c.id,
                "title": c.title,
                "location": doc.title,
                "public_text": c.text,
                "dm_notes": "",
                "npcs": [],
                "keywords": [],
                "flags_required": [],
                "flags_set": [],
                "checks": [],
                "exits": exits,
            }
        )
    if not scenes:
        scenes = [normalize_scene({"id": "start", "title": doc.title, "location": doc.title})]
    return scenes


# ---------------------------------------------------------------- 次级 prompt


def build_script_prompt(doc: ScriptDoc, audience: str = "dm") -> str:
    """生成「次级 prompt」—— 排在 System prompt 之后、随剧本变化的那一层。

    - audience="dm"：完整版，含世界观/语气 + 切片索引（正文交给工具按需读取）。
    - audience="advisor"：公开版，只给背景与约束，不给场景索引与内幕（最小权限 + 防剧透）。
    """
    if audience == "advisor":
        lines = [f"【剧本】《{doc.title}》"]
        if doc.premise:
            lines.append(f"【背景】{doc.premise}")
        lines.append(
            "【约束】你只能围绕本剧本已存在的场景、出口与检定给建议；"
            "不得发明剧本之外的地点、NPC 或物品，也不得透露剧本留给主持人的内幕。"
        )
        return "\n".join(lines)

    lines = [f"【剧本】《{doc.title}》"]
    meta = [
        f"来源 {doc.source}" if doc.source else "",
        "结构化剧本" if doc.structured else "纯文本",
        f"切片 {len(doc.chunks)} 个",
    ]
    lines.append("（" + "　".join(m for m in meta if m) + "）")
    if doc.system:
        lines.append("")
        lines.append(doc.system)
    index = doc.index_chunks()
    if index:
        lines.append("")
        lines.append("【切片索引】")
        # 只给 id + 标题。字数与关键词曾经也列在这里，但那是给「人去挑切片」用的：
        # 一行 40 字，剧本一长（几十个场景）就是几千 token 的**固定开销** —— 每轮
        # 都要重新 prefill 一遍，而模型真正需要的信息（当前场景的原文/出口/检定）
        # 已经在 user 消息里了。地图感留着，体积砍掉。
        for c in index[:MAX_INDEX_CHUNKS]:
            lines.append(f"- {c.id}｜{c.title}")
        if len(index) > MAX_INDEX_CHUNKS:
            lines.append(
                f"（另有 {len(index) - MAX_INDEX_CHUNKS} 个场景未列出："
                "用 lookup_script(关键词) 检索，或 read_scene(场景id) 直读。）"
            )
        lines.append("正文不在此展开：需要某个切片时用 read_scene(场景id) 或 lookup_script(关键词) 按需读取。")
    return "\n".join(lines)
