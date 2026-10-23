"""切分自检与自动迭代：**Agent 自己判定"切干净了没有"，不合格就自己重切。**

为什么要这一层：人工翻 2000 个块不现实，而"切歪了"是有客观特征的 —— 断名、超长、块里塞着
多个条目、碎片、句子被切断。这些都能写成断言，断言不通过就修，修完再断言，直到通过为止。
这就是"自动迭代到正确"的具体形式，而不是等用户来指出"狗皮大衣"。

五类缺陷（都有明确判据，不靠感觉）：

| code | 判据 | 修法 |
|---|---|---|
| `wrapped_name` | 名字以 `)`/`）` 开头或长度 ≤2（"衣)"） | 与同页上一块合并，重新解析名字 |
| `multi_entry` | 一块里出现 ≥2 个 `名称（NN%）` 条目头 | 按条目头再切 |
| `over_long` | 超过 `max_chars × 1.35` | 按句号切到 `max_chars` 以内 |
| `fragment` | 短于 10 字且不是清单条目 | 并入同页相邻块 |
| `dangling_tail` | 结尾是 `，；：`（句子被切断） | 并入同页下一块 |

跑法：`audit → 有缺陷 → repair → 再 audit`，最多 3 轮；每轮的动作都记进报告，
所以"它凭什么说切干净了"是可复查的。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .outline import _INLINE_HEAD_RE, Block

_SENT_END = ("。", "！", "？", "…", "”", "」", "』", "；")
_TAIL_BAD = ("，", ",", "；", ";")  # 冒号是列表引导语，不算断句
_PRICE_RE = re.compile(r"[$￥]\s*[\d.,]+|\d+(?:\.\d+)?\s*(?:美分|美元|便士|先令|英镑)")


@dataclass
class Defect:
    code: str
    block_id: str
    page: int
    title: str
    detail: str
    severity: str = "warn"


@dataclass
class SelfCheckResult:
    rounds: int = 0
    actions: list[dict] = field(default_factory=list)
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    open_defects: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        n = max(1, self.after.get("blocks", 1))
        hard = {"over_long", "fragment", "wrapped_name", "list_glue"}
        residual = len(self.open_defects) / n
        return {
            "rounds": self.rounds,
            "actions": self.actions,
            "before": self.before,
            "after": self.after,
            "open_defects": self.open_defects,
            # 通过标准：没有"必须修"的缺陷，且残留率 < 0.5%
            "passed": (not any(d["code"] in hard for d in self.open_defects)) and residual < 0.005,
            "residual_rate": round(residual, 4),
        }


def _heads(text: str) -> list:
    """句首的 `名称（NN%）` 才算条目头（句中的是引用，不是条目）。"""
    return [m for m in _INLINE_HEAD_RE.finditer(text or "")
            if m.start() == 0 or text[m.start() - 1] in "。！？\n；"]


def _metrics(blocks: list[Block], max_chars: int) -> dict:
    n = len(blocks) or 1
    lens = sorted(b.char_len for b in blocks)
    return {
        "blocks": len(blocks),
        "avg_len": round(sum(lens) / n, 1),
        "p95_len": lens[int(n * 0.95)] if lens else 0,
        "max_len": lens[-1] if lens else 0,
        "over_long": sum(1 for b in blocks if b.char_len > max_chars * 1.35),
        "multi_entry": sum(
            1 for b in blocks
            if not ((b.meta or {}).get("list_item") or (b.meta or {}).get("row"))
            and len(_heads(b.text)) >= 2
        ),
        "fragments": sum(
            1 for b in blocks
            if b.char_len < 10 and not ((b.meta or {}).get("list_item") or (b.meta or {}).get("row"))
        ),
        "list_glue": sum(
            1 for b in blocks
            if not ((b.meta or {}).get("list_item") or (b.meta or {}).get("row"))
            and len(_PRICE_RE.findall(b.text or "")) >= 3
            and b.char_len > 240
        ),
    }


def audit(blocks: list[Block], max_chars: int) -> list[Defect]:
    out: list[Defect] = []
    for i, b in enumerate(blocks):
        meta = b.meta or {}
        text = (b.text or "").strip()
        title = (b.title or "").strip()
        is_item = bool(meta.get("list_item") or meta.get("row"))

        # 1) 断名：清单/表格行的名字被上/下一行截断（"狗皮大" + "衣)，$19.95"）
        unbalanced = (title.count("(") + title.count("（")) != (title.count(")") + title.count("）"))
        if is_item and (unbalanced or len(title) <= 2):
            out.append(Defect("wrapped_name", b.id, b.page, title, f"名字可疑：{title!r}"))

        # 2) 一块多条：块里还能找到 ≥2 个条目头
        heads = _heads(text)
        if not is_item and len(heads) >= 2 and len(text) > 240:
            out.append(Defect("multi_entry", b.id, b.page, title, f"{len(heads)} 个条目头挤在一块"))

        # 3) 超长
        if b.char_len > max_chars * 1.35:
            out.append(Defect("over_long", b.id, b.page, title, f"{b.char_len} 字 > {int(max_chars*1.35)}"))

        # 4) 碎片
        if b.char_len < 10 and not is_item:
            out.append(Defect("fragment", b.id, b.page, title, f"仅 {b.char_len} 字"))

        # 5) 尾句被切断（结尾是逗号/分号，且不是清单行）
        if not is_item and text and text.endswith(_TAIL_BAD):
            out.append(Defect("dangling_tail", b.id, b.page, title, f"结尾「{text[-12:]}」"))

        # 6) 清单粘连：一块里堆了 ≥3 个价格 —— 说明"名称，价格"的清单没被切开
        #
        # 为什么需要这条：清单识别依赖"连续 ≥3 行"，一旦跨栏阅读顺序被排版打乱
        # （实测 p381 的"女装"分组里混进了"户外服饰""短衬裤"三组商品），连续条件不成立，
        # 整段就掉进段落合并，变成 384 字一块。调分栏算法风险太大（会牵动全书），
        # 而"块里有几个价格"是块自身的属性，自检能直接发现并拆开 —— 这是兜底。
        if not is_item:
            prices = len(_PRICE_RE.findall(text))
            if prices >= 3 and b.char_len > 240:
                out.append(Defect("list_glue", b.id, b.page, title, f"{prices} 个价格挤在一块"))
    return out


# ---------------------------------------------------------------- 修

def _split_by_sentences(text: str, limit: int) -> list[str]:
    """按句号切到 limit 以内；切不动就按 limit 硬切（保证不超长）。"""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for seg in re.split(r"(?<=[。！？…；])", text):
        if cur and len(cur) + len(seg) > limit:
            parts.append(cur)
            cur = seg
        else:
            cur += seg
    if cur:
        parts.append(cur)
    out = []
    for p in parts:
        while len(p) > limit * 1.6:
            out.append(p[:limit])
            p = p[limit:]
        if p:
            out.append(p)
    return out


def _fix_multi_entry(b: Block) -> list[Block]:
    text = b.text or ""
    hits = list(_INLINE_HEAD_RE.finditer(text))
    cuts = sorted({0} | {m.start() for m in hits if m.start() > 0} | {len(text)})
    out = []
    for k in range(len(cuts) - 1):
        seg = text[cuts[k] : cuts[k + 1]].strip()
        if len(seg) < 4:
            if out:
                out[-1].text += seg
            continue
        m = _INLINE_HEAD_RE.match(seg) or _INLINE_HEAD_RE.search(seg[:50])
        title = (m.group("name").strip() if m else b.title)[:24] or b.title
        out.append(
            Block(
                id=f"{b.id}-m{len(out):02d}",
                section_id=b.section_id,
                order=b.order,
                page=b.page,
                page_end=b.page_end,
                title=title.strip("（）() "),
                text=seg,
                meta=dict(b.meta or {}),
            )
        )
    return out or [b]


def repair(blocks: list[Block], defects: list[Defect], max_chars: int) -> tuple[list[Block], list[dict]]:
    """按缺陷类型逐个修。修完的顺序仍然按页码，方便和原文对照。"""
    by_id = {b.id: b for b in blocks}
    actions: list[dict] = []
    out: list[Block] = []
    merge_into_prev: set[str] = set()

    # 先处理"合并类"，避免先切后合来回折腾
    for d in defects:
        if d.code in ("fragment", "dangling_tail", "wrapped_name"):
            merge_into_prev.add(d.block_id)

    for i, b in enumerate(blocks):
        if b.id in merge_into_prev:
            prev = out[-1] if out else None
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            target = prev if (prev and prev.page == b.page) else nxt
            # 合并前看一眼：把目标撑过 max_chars×1.35 就宁可不合（否则越修越长）
            can_merge = target is not None and (
                target.char_len + b.char_len <= max_chars * 1.35
                or d.code == "wrapped_name"  # 断名必须合，它是同一条目
            )
            if can_merge:
                if target is prev:
                    prev.text = (prev.text or "").rstrip() + (b.text or "")
                    prev.page_end = max(prev.page_end, b.page_end)
                else:
                    target.text = (b.text or "") + (target.text or "")
                    target.page = min(target.page, b.page)
                # 清单/表格行合并后名字要重新解析（"狗皮大" + "衣)，$19.95"）
                if target.meta and target.meta.get("list_item"):
                    from .outline import parse_list_line

                    got = parse_list_line((target.text or "").strip())
                    if got:
                        target.meta["name"], target.meta["price"] = got[0], got[1]
                        target.title = got[0]
                actions.append({"code": d.code, "block": b.id, "into": target.id, "page": b.page})
                continue
        out.append(b)

    # 再处理"切分类"
    final: list[Block] = []
    for b in out:
        codes = {d.code for d in defects if d.block_id == b.id}
        if "list_glue" in codes:
            parts = _fix_list_glue(b)
            if len(parts) > 1:
                final.extend(parts)
                actions.append({"code": "list_glue", "block": b.id, "page": b.page, "into": len(parts)})
                continue
        if "multi_entry" in codes:
            final.extend(_fix_multi_entry(b))
            actions.append({"code": "multi_entry", "block": b.id, "page": b.page,
                            "into": "split"})
            continue
        if "over_long" in codes:
            segs = _split_by_sentences(b.text or "", int(max_chars))
            for k, seg in enumerate(segs):
                final.append(
                    Block(id=f"{b.id}-s{k}", section_id=b.section_id, order=b.order, page=b.page,
                          page_end=b.page_end, title=b.title if k == 0 else f"{b.title}（续{k}）",
                          text=seg, meta=dict(b.meta or {}))
                )
            actions.append({"code": "over_long", "block": b.id, "page": b.page, "into": len(segs)})
            continue
        final.append(b)

    final.sort(key=lambda x: (x.page, x.order))
    return final, actions


def _fix_list_glue(b: Block) -> list[Block]:
    """把"一堆商品粘在一块"的块按行拆成一条一块。

    分组名（"户外服饰："这种不含价格的短行）不单独成块，而是挂到**它下面第一条商品**
    的文本前面 —— 因为"卡其牛仔布，$1.79"离开"户外服饰"就没有分类信息了。

    拆出来的块继承原块的 kind/subtype（清单本来就是工具类），并把 `list_item` 置真，
    这样它们不会在下一轮被当成段落又合回去。
    """
    lines = [x.strip() for x in (b.text or "").split("\n") if x.strip()]
    if len(lines) < 2:
        return [b]

    out: list[Block] = []
    group = ""
    for ln in lines:
        if _PRICE_RE.search(ln):
            body = f"{group}\n{ln}".strip() if group else ln
            name = ln
            try:
                from .outline import parse_list_line

                got = parse_list_line(ln)
                if got:
                    name = got[0]
            except Exception:
                pass
            title = f"{group}·{name}"[:24] if group else name[:24]
            out.append(
                Block(
                    id=f"{b.id}-l{len(out)}",
                    section_id=b.section_id,
                    order=b.order,
                    page=b.page,
                    page_end=b.page_end,
                    title=title,
                    text=body,
                    kind=b.kind or "tool",
                    subtype=b.subtype or "物价",
                    confidence=b.confidence,
                    meta={**(b.meta or {}), "list_item": True, "group": group},
                )
            )
            group = ""
        else:
            group = ln[:12]  # 只可能是分组名；太长的行说明不是清单，交给别的规则
    return out or [b]


def run(blocks: list[Block], max_chars: int, max_rounds: int = 4) -> tuple[list[Block], SelfCheckResult]:
    """自检 → 修 → 再自检，直到没有缺陷或到轮数上限。"""
    res = SelfCheckResult(before=_metrics(blocks, max_chars))
    cur = blocks
    for r in range(1, max_rounds + 1):
        defects = audit(cur, max_chars)
        res.rounds = r
        if not defects:
            res.after = _metrics(cur, max_chars)
            return cur, res
        cur, acts = repair(cur, defects, max_chars)
        for a in acts:
            a["round"] = r
        res.actions.extend(acts)
    res.after = _metrics(cur, max_chars)
    res.open_defects = [
        {"code": d.code, "page": d.page, "title": d.title, "detail": d.detail}
        for d in audit(cur, max_chars)
    ]
    return cur, res
