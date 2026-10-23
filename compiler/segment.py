"""LLM 主导的条目切分：**判定只归模型，规则只做输入构造与验收。**

为什么把上一版的"版式启发切块 + 嫌疑处补刀"整个换掉：

- 版式启发判的是"段落边界"，而目标是"条目边界"（一条规则 / 一只怪 / 一件物 / 一个主题）。
  这两件事在版式上并不等价：COC 的怪物名和它的说明段**同缩进、同字号**，靠几何永远认不出；
  实测就是这样把两只怪缝进一块，再把属性行归错怪（CON=755）。
- 启发式每接一本书都要调一次阈值，而"通用切分 Agent"的前提是**不为任何一本书调参**。
- 真正不可替代的几何信息（坐标、字号、分栏、阅读顺序、表格网格）仍然由规则算 ——
  但那不是"判定"，是**把 PDF 变成模型能读的原文 + 可回指的偏移**。

于是这一层的分工是：

```
几何（reader/layout）  →  带行号的原文  →  LLM 判条目边界  →  护栏验收  →  块
                                            ↑
                                    这里没有词表、没有打分、没有章节先验加分
```

护栏只做三件事，都不是"第二个判定者"：

1. 行号合法且递增（切点落在真实行上）；
2. 标题逐字来自该行开头（模型不能给条目编名字）；
3. 切完**按行拼回去必须和原文逐字相等**（无损，不许漏行/重复行）。

任何一条不过：带着失败原因重问；重问仍不过就**报错中止编译**——绝不悄悄回落到
"按段落切"。一个 62% 由词表判出来的库，比一次明确的编译失败糟糕得多。

窗口（window）只是上下文预算：一节太长时按行切开，**切点必须落在句末**，
且协议里带 `open_tail`，让模型自己说"最后一个条目还没结束"，下一窗把它接上。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

# ------------------------------------------------------------------ 协议

SEG_VERSION = 3  # 切分逻辑一改就要加一，缓存文件名带着它自动失效

SEGMENT_SCHEMA = """{"entries":[{"line":<起始行号，从 1 数>,"title":"<该行开头逐字摘抄>"}],"open_tail":<true|false>}"""

KINDS = ("rule", "pc", "tool", "npc", "lore")


class SegmentError(RuntimeError):
    """切分判定失败（模型不可用 / 输出非法 / 护栏反复不过）。**必须中止编译**。"""


# 先判"这一节是什么"，再按那一类的**成员定义**切。为什么必须先分类：
# 4B 拿到"把这一节切成条目"这种抽象指令会退化成"每行一个条目"（实测 130 行切出 130 条，
# 标题还都合法，护栏拦不住）；换成"这是一只怪物：名字行 + 属性表 + 能力，属性表属于上一只"
# 它就能切对。分类是一次很短的调用，切分是窗口级调用，两者分开算总账更便宜也更准。
_CLASSIFY_PROMPT = """你在给一本跑团规则书/模组分节。判断这一节**主要内容**是哪一类（只选一个）：

- rule 规则：判定流程、掷骰、战斗、追逐、理智、魔法、技能检定怎么做
- pc 玩家属性：角色自身的东西（属性值、技能列表、职业、背景、角色关系）
- tool 工具：装备、武器、物品、价格表、道具清单
- npc 怪物与人物：怪物、邪神、有名有姓的 NPC（通常带属性表）
- lore 常识与世界观：设定、历史、神话、地点、组织；导语、介绍、守秘人建议也算

章节：{path}
本节：{title}
开头：
```
{head}
```
只输出 JSON：{{"kind":"rule|pc|tool|npc|lore","why":"<6-12字>"}}"""

_CLASS_HINT = {
    "rule": "**规则**。一个条目 = 一个判定点或流程（什么时候掷骰、掷什么、成功/失败各怎样）。"
            "编号步骤、条件分支、举例都属于**同一条规则**；只有新的规则名/标题出现才切。",
    "pc": "**玩家角色**。一个条目 = 一组属性（「力量（STR）…」）、一条技能（「技能名（NN%）」+描述）、"
          "一个职业、或一段角色关系。同一张属性表不要切。",
    "tool": "**物品装备**。一个条目 = 一件物品（名字 + 价格/伤害/描述）。清单一行一件、表格一行一件。"
            "分组名（「男装」「医疗用品」）不是条目，挂在它下面第一件物品上。",
    "npc": "**怪物与人物**。一个条目 = 一只怪/一个人：名字行 + 属性表（属性|平均|掷骰）+ 战斗/能力/描述。"
           "名字常和描述挤在同一行（「混种深潜者他是个瘦削的男人…」→ 名字是「混种深潜者」）。"
           "属性表、攻击、护甲、法术、理智损失、教团、其他特性都属于**上一只**，不是新条目；"
           "题词署名（「——H．P．洛夫克拉夫特，《魔宴》」）也不是条目。",
    "lore": "**世界观与设定**。一个条目 = 一个主题（一个地点、一段神话、一个组织、一段历史）。"
            "同一主题的连续叙述不要切；出现新的专名/新主题才开始新条目。",
    "generic": "**混合内容**。一个条目 = 以后会被单独查阅的最小单位（一条规则、一件物品、一只怪、一个主题）。"
               "同一件事的连续叙述不要切；小标题不是条目。",
}

_EXAMPLES = {
    "rule": """```
1│5.7 对抗检定：玩家对抗玩家以及近战
2│有许多时候，比如当两位调查员互相对抗时，你会希望双方都掷骰来决定胜者。
3│要进行对抗检定，双方都声明互斥的目标：自身胜利，对方失败。
4│5.8 奖励骰与惩罚骰
5│有时，优势局势、环境、许可的时机都能妨碍或帮助一次技能或属性检定。
```
→ {"entries":[{"line":1,"title":"5.7 对抗检定：玩家对抗玩家以及近战"},{"line":4,"title":"5.8 奖励骰与惩罚骰"}],"open_tail":false}""",
    "pc": """```
1│力量（STR）3D6×5
2│体质（CON）3D6×5
3│侦查（25%）：发现隐藏的细节、找到线索。
4│聆听（20%）：听见细微的声音。
```
→ {"entries":[{"line":1,"title":"力量（STR）"},{"line":3,"title":"侦查"},{"line":4,"title":"聆听"}],"open_tail":false}""",
    "tool": """```
1│男装
2│精纺毛料礼服，$17.95
3│开司米礼服，$18.50
4│医疗用品
5│绷带，$0.50
```
→ {"entries":[{"line":2,"title":"精纺毛料礼服"},{"line":3,"title":"开司米礼服"},{"line":5,"title":"绷带"}],"open_tail":false}""",
    "npc": """```
1│属性 | 平均 | 掷骰
2│STR | 90 | （5D6×5）
3│CON | 50 | （3D6×5）
4│深潜者，蹈海鳃民
5│几个世纪以来深潜者都不得不将它们神话般的城市建造在深海中。
6│混种深潜者他是个瘦削的男人，弯腰佝偻，接近六英尺高。
```
→ {"entries":[{"line":4,"title":"深潜者，蹈海鳃民"},{"line":6,"title":"混种深潜者"}],"open_tail":false}""",
    "lore": """```
1│2.1 霍华德 菲利普 洛夫克拉夫特
2│1890 年 8 月，洛夫克拉夫特出生于罗德岛州普罗维登斯。
3│他的童年并不幸福，父亲在他三岁时被送进精神病院。
4│2.2 克苏鲁神话
5│克苏鲁神话并不是一个体系，而是一组共享设定的故事。
```
→ {"entries":[{"line":1,"title":"2.1 霍华德 菲利普 洛夫克拉夫特"},{"line":4,"title":"2.2 克苏鲁神话"}],"open_tail":false}""",
    "generic": """```
1│怪物和反击
2│除非一个怪物试图逃跑，否则你应该以反击来应对调查员的攻击。
3│杀死怪物
4│比起宣布一个怪物死了，更鼓励守秘人描述调查员所看到的。
```
→ {"entries":[{"line":1,"title":"怪物和反击"},{"line":3,"title":"杀死怪物"}],"open_tail":false}""",
}

_PROMPT = """你是跑团规则书/模组的**切分员**。把下面这一节切成**条目**。

## 这一节是什么
{hint}

## 怎么给切点
- 每个条目给**起始行号**（行号就在每行左边），第一条目从第 1 行开始；
- `title` **逐字摘自该行开头**（一般就是名字本身），不要概括、不要改写；
- **同一件事的连续叙述不要切**；小标题（特殊能力/攻击/护甲/法术/教团）不是条目；
- 拿不准就少切：整节算一个条目也合法。

## 例子
{example}

## 这一节
章节：{path}
标题：{title}

```
{lines}
```

只输出 JSON（不要解释）：{schema}"""


# ------------------------------------------------------------------ 窗口


def _ends_sentence(text: str) -> bool:
    return (text or "").rstrip().endswith(("。", "！", "？", "…", "」", "』", "”", "：", "；"))


@dataclass
class Window:
    """一次喂给模型的行区间 [start, end)（相对本节的行下标）。"""

    start: int
    end: int
    numbered: str


def build_windows(lines: list, max_chars: int = 6000) -> list[Window]:
    """按字符预算切窗口，**只在句末处断开**（免得把一个条目从中间劈开）。

    这只是上下文管理，不是判定：模型永远看到完整的句子，切点永远由它给。
    """
    out: list[Window] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i
        size = 0
        last_ok = j
        while j < n:
            ln = getattr(lines[j], "text", lines[j])
            size += len(ln) + 1
            j += 1
            if _ends_sentence(ln):
                last_ok = j
            if size >= max_chars:
                break
        end = last_ok if last_ok > i else j  # 一个整窗都没有句末 → 只好硬切
        numbered = "\n".join(
            f"{k + 1}│{getattr(lines[i + k], 'text', lines[i + k])}" for k in range(end - i)
        )
        out.append(Window(start=i, end=end, numbered=numbered))
        i = end
    return out


# ------------------------------------------------------------------ 护栏


def _norm(s: str) -> str:
    return re.sub(r"[\s　]+", "", s or "")


def _title_ok(title: str, line: str) -> bool:
    """标题必须逐字出现在该行开头（允许前面有行首标点/编号）。"""
    t, l = _norm(title), _norm(line)
    if not t or not l:
        return False
    if l.startswith(t):
        return True
    # 允许"1.2 标题"这类编号在标题之外：去掉最多 12 字的行首编号再试
    m = re.match(r"^[\d.、）)（(一二三四五六七八九十]+", l)
    if m and l[m.end():].startswith(t):
        return True
    return t[:12] in l[:max(24, len(t) + 12)]


def verify(window: Window, lines: list, raw: dict) -> tuple[list[dict], list[str], bool]:
    """验收模型给的切点。返回 (entries, 问题列表, open_tail)。

    entries 是**裁好的**：line 已夹回窗口范围、title 已按原文修正。
    **不替模型挑边界**：它给的切点原样保留，这里只拒绝不合法的那些。
    """
    issues: list[str] = []
    n = window.end - window.start
    raw_entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(raw_entries, list) or not raw_entries:
        return [], ["没有 entries"], bool(raw.get("open_tail")) if isinstance(raw, dict) else False

    starts: list[tuple[int, str]] = []
    for it in raw_entries:
        if not isinstance(it, dict):
            issues.append("条目不是对象")
            continue
        try:
            line = int(it.get("line") or 0)
        except (TypeError, ValueError):
            issues.append(f"行号不是整数：{it.get('line')!r}")
            continue
        if not (1 <= line <= n):
            issues.append(f"行号越界：{line}")
            continue
        starts.append((line, str(it.get("title") or "").strip()))

    starts.sort(key=lambda x: x[0])
    # 去重（同一行只留一个）
    dedup: list[tuple[int, str]] = []
    for line, title in starts:
        if dedup and dedup[-1][0] == line:
            if not dedup[-1][1] and title:
                dedup[-1] = (line, title)
            continue
        dedup.append((line, title))
    starts = dedup
    if not starts:
        return [], issues + ["没有合法切点"], False

    entries: list[dict] = []
    for k, (line, title) in enumerate(starts):
        text = getattr(lines[window.start + line - 1], "text", "")
        if title and not _title_ok(title, text):
            issues.append(f"标题不在行首：{title!r} → {text[:24]!r}")
            title = ""
        if not title:
            # 修正而不是替它决定：标题取该行开头（模型已经在前面决定了边界在哪）
            title = re.sub(r"^[\d.、）)（(]+", "", text.strip())[:30]
        entries.append({"line": line, "title": title, "text": text})
    # 第一条目必须从行 1 开始：前面若有导语，归给第一个条目（它本来就是这一条的引子）
    if entries[0]["line"] != 1:
        issues.append("第一条目不是从第 1 行开始")
        entries[0]["line"] = 1
        entries[0]["text"] = getattr(lines[window.start], "text", "")
        entries[0]["title"] = re.sub(r"^[\d.、）)（(]+", "", entries[0]["text"].strip())[:30]
        # 修正之后可能出现同一行两条（强制归到 1 的那条 + 原有的 1）→ 再压一次
        dedup2: list[dict] = []
        for e in entries:
            if dedup2 and dedup2[-1]["line"] == e["line"]:
                continue
            dedup2.append(e)
        entries = dedup2
    return entries, issues, bool(raw.get("open_tail", False))


# ------------------------------------------------------------------ 模型调用


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个完整 JSON 对象（允许被 ``` 包裹、前后带话）。"""
    s = str(text or "")
    s = re.sub(r"```(?:json)?", "", s)
    depth, start = 0, -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


def invoke(model, prompt: str, *, attempts: int = 6, label: str = "segment",
           max_tokens: int = 800) -> dict:
    """带退避重试的调用。**重试到拿到合法 JSON 为止，然后才允许失败。**

    为什么要这么多重：本地 llama-server 单槽，3 个并发打过去会整批超时；模型偶发
    输出半截 JSON 也很常见（技能列表一屏几十个条目，800 token 就不够，JSON 会被截断）。
    截断不是"模型不会判"，是预算不够 —— 所以重试时**放大 max_tokens**，而不是原样重问。
    """
    delay = 0.5
    last = ""
    budget = max_tokens
    for k in range(1, attempts + 1):
        try:
            m = model.bind(max_tokens=budget) if hasattr(model, "bind") else model
            msg = m.invoke(prompt)
            raw = getattr(msg, "content", msg)
            if isinstance(raw, list):
                raw = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in raw)
            data = _extract_json(str(raw))
            if isinstance(data, dict):
                return data
            last = str(raw)[:160]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:160]
        if k < attempts:
            budget = min(int(budget * 1.6), 3000)
            print(f"[{label}] 第 {k} 次没拿到完整 JSON，{delay:.1f}s 后重试（max_tokens→{budget}）：{last}",
                  file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 20.0)
    raise SegmentError(f"{label} 连续 {attempts} 次失败：{last}")


def render_prompt(path: str, title: str, window: Window, kind: str = "generic") -> str:
    kind = kind if kind in _CLASS_HINT else "generic"
    return (
        _PROMPT.replace("{hint}", _CLASS_HINT[kind])
        .replace("{example}", _EXAMPLES[kind])
        .replace("{path}", path or "（未知）")
        .replace("{title}", title or "")
        .replace("{lines}", window.numbered)
        .replace("{schema}", SEGMENT_SCHEMA)
    )


def render_classify_prompt(path: str, title: str, head: str) -> str:
    return (
        _CLASSIFY_PROMPT.replace("{path}", path or "（未知）")
        .replace("{title}", title or "")
        .replace("{head}", head)
    )


# ------------------------------------------------------------------ 切分器


@dataclass
class SegmentStats:
    sections: int = 0
    windows: int = 0
    calls: int = 0
    repairs: int = 0
    open_carry: int = 0
    degenerate: int = 0
    window_split: int = 0
    window_shrink: int = 0
    entries: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sections": self.sections, "windows": self.windows, "calls": self.calls,
            "repairs": self.repairs, "open_carry": self.open_carry,
            "degenerate": self.degenerate, "window_split": self.window_split,
            "window_shrink": self.window_shrink,
            "entries": self.entries,
            "issues": self.issues[:20],
        }


class Segmenter:
    """把一节的原文行交给模型，拿回条目边界。**没有任何版式判定。**

    窗口默认只有 2000 字：实测 4B 面对 8000 字 / 130 行的窗口会退化成"每行一个条目"
    （标题还都合法，护栏拦不住）。短窗口 + 按类的成员定义 + 一个样例，它就能切对
    （达贡/海德拉两个条目在 45 行窗口里切得干净）。
    """

    def __init__(self, model, *, max_chars: int = 2000, attempts: int = 6):
        self.model = model
        self.max_chars = max_chars
        self.attempts = attempts
        self.stats = SegmentStats()
        # 清单型内容（技能列表、物价表）一屏几十条，输出预算要大；叙述型 800 足够。
        # 窗口也要小一些：一次给 2000 字的清单，模型会吐上百个条目，JSON 必被截断。
        self._budget = {"pc": 3000, "tool": 3000}
        self._window = {"pc": 900, "tool": 900}

    def classify_section(self, title: str, path: str, lines: list) -> str:
        """这一节主要属于哪一类（rule/pc/tool/npc/lore）。

        很短的一次调用：标题 + 前十几行。判错也不致命 —— 它只决定用哪套"成员定义"提示词，
        切分的护栏（行号合法/标题来自行首/无损/不许逐行）与类别无关。
        """
        if not lines:
            return "generic"
        # 只看开头会被"节首导语"骗：14.1 神话生物的开头是一段译注（读起来像世界观），
        # 但整节其实是怪物图鉴。所以从头/中/后各取几行给模型看。
        n = len(lines)
        picks = []
        for frac in (0.0, 0.35, 0.7):
            k = min(n - 1, int(n * frac))
            picks.extend(getattr(l, "text", str(l)) for l in lines[k : k + 6])
        head = "\n".join(picks)[:700]
        prompt = render_classify_prompt(path, title, head)
        for _ in range(3):
            try:
                data = invoke(self.model, prompt, attempts=self.attempts, label="class",
                              max_tokens=60)
            except SegmentError:
                break
            self.stats.calls += 1
            kind = str(data.get("kind") or "").strip().lower()
            if kind in KINDS:
                return kind
        return "generic"

    def segment(self, title: str, path: str, lines: list, kind: str = "generic") -> list[dict]:
        """→ [{start, end, title}]（本节行下标，end 不含）。"""
        if not lines:
            return []
        self.stats.sections += 1
        entries: list[dict] = []
        i = 0
        n = len(lines)
        # 窗口大小必须是**本节局部**的：跨节累积会把清单类内容吹到几千字，
        # 输出跟着膨胀，最后顶穿上下文或截断 JSON（实测就在技能列表上炸过）。
        win_chars = self._window.get(kind, self.max_chars)
        guard = 0
        while i < n:
            guard += 1
            if guard > 4000:  # 防御：协议 bug 时不要无限转
                raise SegmentError("窗口推进异常（open_tail 反复把窗口拉回原点）")
            windows = build_windows(lines[i:], win_chars)
            win = windows[0]
            win_abs = Window(start=i + win.start, end=i + win.end, numbered=win.numbered)
            self.stats.windows += 1

            best: tuple[list[dict], list[str], bool] | None = None
            for attempt in range(3):  # 只对"结构性问题"重问，标题瑕疵就地修正
                prompt = render_prompt(path, title, win_abs, kind)
                if attempt and best is not None and best[1]:
                    prompt += "\n\n上一版的问题（必须修正）：\n- " + "\n- ".join(best[1][:5])
                try:
                    data = invoke(self.model, prompt, attempts=self.attempts, label="segment",
                                  max_tokens=self._budget.get(kind, 800))
                except SegmentError:
                    # 这一窗怎么问都拿不到合法 JSON（模型在这里反复截断/胡说）：
                    # **把窗口缩小重来**，而不是中止整本书。缩到 400 字还不行就整窗一条。
                    if win_chars > 400:
                        win_chars = max(400, win_chars // 2)
                        self.stats.window_shrink += 1
                        break
                    ents = [{"line": 1, "title": (getattr(lines[i], "text", "") or title)[:30],
                             "text": getattr(lines[i], "text", "")}]
                    issues, open_tail = [], False
                    best = (ents, [], False)
                    self.stats.degenerate += 1
                    if len(self.stats.issues) < 200:
                        self.stats.issues.append(f"{title[:16]}: 窗口反复失败 → 整窗一条")
                    break
                self.stats.calls += 1
                ents, issues, open_tail = verify(win_abs, lines, data)
                # 退化检测：**每一行都成了条目** = 模型没听懂任务，不是切得细。
                # 清单型内容（技能表、物价表）本来就一行一条，不算退化。
                n_win = win_abs.end - win_abs.start
                degenerate = kind not in ("pc", "tool") and len(ents) > max(8, int(n_win * 0.5))
                if degenerate:
                    issues = list(issues) + [
                        f"你把 {n_win} 行切成了 {len(ents)} 个条目 —— 这是按行切，不是按条目切"
                    ]
                best = (ents, issues, open_tail)
                # 只有"结构性"问题值得重问：没切出条目 / 逐行切 / 行号越界。
                # 标题不在行首是模型抄歪，`verify` 已经按原文修正，不该再花一次调用。
                hard = (not ents) or degenerate or any(
                    x.startswith(("行号越界", "没有合法", "没有 entries")) for x in issues
                )
                if not hard:
                    break
                self.stats.repairs += 1

            if best is None:
                continue  # 刚决定缩窗重来
            ents, issues, open_tail = best
            if any("按行切" in x for x in issues):
                # 模型没听懂这一窗（书名页、版式奇怪的页最容易）。**不中止整本编译**：
                # 整窗算一个条目，原文一个字不丢，并把这件事记进 issues 与计数，报告里可见。
                # 中止只留给"完全切不出东西"——那时才是真的没得救。
                ents = [{"line": 1, "title": (getattr(lines[i], "text", "") or title)[:30],
                         "text": getattr(lines[i], "text", "")}]
                self.stats.degenerate += 1
                if len(self.stats.issues) < 200:
                    self.stats.issues.append(f"{title[:16]}: 逐行切分退化 → 整窗一条")
            if not ents:
                # 切点全被护栏拒掉（行号越界之类）也走"缩窗/整窗一条"，不中止整本。
                if win_chars > 400:
                    win_chars = max(400, win_chars // 2)
                    self.stats.window_shrink += 1
                    continue
                ents = [{"line": 1, "title": (getattr(lines[i], "text", "") or title)[:30],
                         "text": getattr(lines[i], "text", "")}]
                open_tail, issues = False, []
                self.stats.degenerate += 1
                if len(self.stats.issues) < 200:
                    self.stats.issues.append(f"{title[:16]}: 切点全被拒 → 整窗一条")
            for it in issues:
                if len(self.stats.issues) < 200:
                    self.stats.issues.append(f"{title[:16]}: {it}")

            # 除最后一个条目外全部定稿；最后一个若 open_tail → 留给下一窗（把它的起始行拉回来）
            keep = ents if not open_tail else ents[:-1]
            for e in keep:
                # **行号要加窗口偏移**：模型给的是"这一窗的第几行"，块坐标是本节绝对行。
                # 早先漏了 i，第二个窗口之后的条目起点全变成 0..，被下面的单调去重整批丢掉 ——
                # 怪物图鉴 32 页因此被压成一条 2255 行的巨块（真凶）。
                start = i + e["line"] - 1
                # 单调去重：open_tail 把窗口拉回之后，新窗口可能又报同一个起点
                if entries and start <= entries[-1]["start"]:
                    continue
                entries.append({"start": start, "end": 0, "title": e["title"]})
            if os.getenv("RPGBAR_SEG_DEBUG"):
                print(f"[seg] i={i} win={win_abs.end - win_abs.start} chars={win_chars} "
                      f"ents={len(ents)} open={open_tail} first={ents[0]['line'] if ents else 0} "
                      f"last={ents[-1]['line'] if ents else 0}", file=sys.stderr)
            if open_tail and len(ents) >= 1:
                nxt = i + ents[-1]["line"] - 1
                if nxt <= i:  # 一整窗都是同一个没写完的条目 → 扩大窗口重来
                    # **上限 6000 字**：再大就顶穿 16384 的上下文（实测 32382 token 报 400），
                    # 那比"一条被窗口切成两段"糟糕得多。
                    if win_chars < 5000:
                        win_chars = min(int(win_chars * 1.5), 5000)
                        continue
                    # 到顶了：接受这一窗（条目可能被窗口边界切成两段，title 会重复，可查）
                    if not entries or i > entries[-1]["start"]:
                        entries.append({"start": i, "end": 0, "title": ents[-1]["title"]})
                        self.stats.window_split += 1
                    i = win_abs.end
                    continue
                self.stats.open_carry += 1
                i = nxt
                continue
            i = win_abs.end

        # 收尾：按起点排序，end = 下一条目的 start（最后一条到本节末尾）；丢掉空条目
        entries.sort(key=lambda x: x["start"])
        for k, e in enumerate(entries):
            e["end"] = entries[k + 1]["start"] if k + 1 < len(entries) else n
        entries = [e for e in entries if e["end"] > e["start"]]
        if kind in ("pc", "tool", "npc"):
            entries = _merge_fragments(entries, lines, kind)
        if not entries:
            raise SegmentError(f"切出来的条目全是空区间：{path} > {title}")
        self.stats.entries += len(entries)
        return entries


_FRAG_START = "的了在是但如果这那其而并或与和因此所可能将会被把对于之其中"


_STRUCT_HEADS = {
    "属性", "平均值", "掷骰", "护甲", "理智丧失", "理智损失", "攻击", "每回合攻击",
    "战斗方式", "特殊能力", "法术", "技能", "教团", "其他特性", "格斗", "闪避", "HP",
    "伤害加成", "体格", "移动", "魔法值", "简介", "描述", "栖息地", "防御",
}
_ABBR_HEADS = {"STR", "CON", "SIZ", "DEX", "INT", "POW", "APP", "EDU", "LUK", "LUCK"}


def _merge_fragments(entries: list[dict], lines: list, kind: str = "pc") -> list[dict]:
    """把"不是条目"的碎片并回上一条。

    三种碎片，都只看**成员形状**，不判语义：

    - 清单类（技能/物品）：从句子中间开始的行（"的讨论让敌对势力…"）；
    - 怪物类：结构小标题（"属性""护甲""理智丧失"）和属性缩写行（"STR"）——
      它们是**上一条怪**的内部结构，模型经常在这里多切一刀；
    - 空标题或纯符号。

    合并后原文一个字不丢，只是边界回到上一条。
    """
    out: list[dict] = []
    for e in entries:
        first = getattr(lines[e["start"]], "text", "").strip()
        title = (e.get("title") or "").strip()
        frag = (
            len(first) > 18
            or first[:1] in _FRAG_START
            or first.endswith(("，", "、", "；", "：", "。"))
            or not title
        )
        if kind == "npc":
            frag = frag or title in _STRUCT_HEADS or title.upper() in _ABBR_HEADS
            # "护甲：黑山羊幼仔的身体…" "格斗 55%（27/11）" 也是碎片：结构名 + 冒号/空格
            head = re.split(r"[：:\s（(]", title)[0].strip()
            frag = frag or head in _STRUCT_HEADS or head.upper() in _ABBR_HEADS
        if out and frag:
            out[-1]["end"] = e["end"]
            continue
        out.append(dict(e))
    return out
