"""大厅：房间的「公开信息」与建房 / 改设置的参数清洗。

为什么要有这一层：在此之前房间是**隐式**的 —— 第一个用房间码 `join` 的人把房间
建出来，房号只在 `welcome` 里回给本人。没有任何地方能看到「现在有哪些房间」，
所以陌生人根本无从加入。大厅补齐的就是这块：

- 建房时带上房主填的元数据：**房间名 / 备注 / 人数上限 / 公开或私密 / 可选密码**；
- `GET /api/rooms` 列出**公开**房间，陌生人据此挑一间进；
- 私密房间不进列表，只能凭房间码（+ 密码）进。

本模块刻意只放**纯逻辑**（常量、清洗、校验、投影），不 import `main` ——
否则会和 `Room` 形成环。

关于房间容量：上限默认取**剧本声明的人数**（剧本 JSON 里可选的 `players` 段，
见 `script_loader.normalize_players`），房主可以在建房时覆盖。剧本没写这个段时
按 4 人（正好对应界面上的四个职业）。
"""
from __future__ import annotations

from .script_loader import DEFAULT_PLAYERS, MAX_PLAYERS_LIMIT

# ---- 房间阶段 ----
# 等待中：人还没到齐，房主在等；此时**不能行动**（没有开局，也没有剧情可推进）
# 进行中：房主点了「开始」，DM 已经出场；此后进来的算「中途加入」
PHASE_WAITING = "waiting"
PHASE_PLAYING = "playing"
PHASES = (PHASE_WAITING, PHASE_PLAYING)

PHASE_LABEL = {PHASE_WAITING: "等待中", PHASE_PLAYING: "进行中"}

# ---- 可见性 ----
VIS_PUBLIC = "public"    # 进大厅列表，陌生人可直接进
VIS_PRIVATE = "private"  # 不上列表，只能凭房间码（+ 密码）进
VISIBILITIES = (VIS_PUBLIC, VIS_PRIVATE)

# ---- 字段长度上限（都是**字符**数；超出就截断，不报错）----
MAX_ROOM_NAME = 24
MAX_NOTE = 80
MAX_PASSWORD = 32

DEFAULT_MIN_PLAYERS = DEFAULT_PLAYERS["min"]
DEFAULT_MAX_PLAYERS = DEFAULT_PLAYERS["max"]
# 房间容量：下线取剧本给的 min（但至少 1），上线取硬上限
HARD_MAX_PLAYERS = MAX_PLAYERS_LIMIT


def clamp_players(value, lo: int, hi: int):
    """把人数夹进 [lo, hi]；给不出数字就返回 None（交给调用方用默认值）。"""
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        try:
            n = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
    return max(lo, min(hi, n))


def _text(value, limit: int) -> str:
    """单行文本：去空白、折叠内部空白、截断。非字符串一律当空。"""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def clean_create(payload, script_players=None):
    """清洗建房参数 → (params, error)。

    `script_players` 是房主所选剧本声明的 `{min,max,recommended}`，用来给
    `max_players` 定默认值与下限。返回值里**一定是完整的一套**（缺的用默认值补齐），
    这样 `Room` 直接用就行。
    """
    payload = payload if isinstance(payload, dict) else {}
    limits = dict(DEFAULT_PLAYERS)
    if isinstance(script_players, dict):
        limits.update({k: v for k, v in script_players.items() if isinstance(v, int)})

    lo = max(1, int(limits.get("min") or 1))
    hi = max(lo, min(HARD_MAX_PLAYERS, int(limits.get("max") or DEFAULT_MAX_PLAYERS)))
    default_max = max(lo, min(hi, int(limits.get("recommended") or hi)))

    max_players = clamp_players(payload.get("max_players"), lo, hi)
    if max_players is None:
        max_players = default_max

    visibility = payload.get("visibility")
    visibility = visibility if visibility in VISIBILITIES else VIS_PUBLIC

    return {
        "name": _text(payload.get("name"), MAX_ROOM_NAME),
        "note": _text(payload.get("note"), MAX_NOTE),
        "max_players": max_players,
        "min_players": lo,
        "visibility": visibility,
        "password": _text(payload.get("password"), MAX_PASSWORD),
        "script": payload.get("script") if isinstance(payload.get("script"), str) else "",
    }, None


def clean_settings(payload, current, script_players=None):
    """清洗「改设置」补丁 → patch（只含白名单字段，没提的字段不动）。

    `clear_password=True` 是**显式清空**密码的唯一方式 —— 密码字段没出现时保持原值，
    免得房主改个备注就把密码弄丢了（与模型槽位的 `api_key` 同一套约定）。
    """
    if not isinstance(payload, dict):
        return {}
    out = {}
    if "name" in payload:
        out["name"] = _text(payload.get("name"), MAX_ROOM_NAME)
    if "note" in payload:
        out["note"] = _text(payload.get("note"), MAX_NOTE)
    if "visibility" in payload:
        v = payload.get("visibility")
        if v in VISIBILITIES:
            out["visibility"] = v
    if "password" in payload:
        out["password"] = _text(payload.get("password"), MAX_PASSWORD)
    if payload.get("clear_password"):
        out["password"] = ""
    if "max_players" in payload:
        lo = max(1, int((current or {}).get("min_players") or 1))
        hi = max(lo, min(HARD_MAX_PLAYERS, int((script_players or {}).get("max") or HARD_MAX_PLAYERS)))
        n = clamp_players(payload.get("max_players"), lo, hi)
        if n is not None:
            out["max_players"] = n
    return out


def password_ok(stored: str, given) -> bool:
    """房间密码校验：没设密码恒通过；设了就要字符串精确匹配。"""
    stored = stored or ""
    if not stored:
        return True
    return isinstance(given, str) and given == stored


def build_public(*, code, name, note, script_title, owner, online, max_players,
                 phase, visibility, has_password, created_at):
    """一个房间对外可见的信息（列表项与 `room` 事件共用同一份形状）。

    ⚠️ **绝不能把密码本身放进来** —— 这里只有 `has_password` 这个布尔。
    """
    return {
        "code": code,
        "name": name or script_title,
        "note": note or "",
        "script": script_title,
        "owner": owner or "",
        "online": int(online or 0),
        "max_players": int(max_players or DEFAULT_MAX_PLAYERS),
        "phase": phase if phase in PHASES else PHASE_WAITING,
        "phase_label": PHASE_LABEL.get(phase, PHASE_LABEL[PHASE_WAITING]),
        "visibility": visibility if visibility in VISIBILITIES else VIS_PUBLIC,
        "has_password": bool(has_password),
        "created_at": float(created_at or 0),
    }
