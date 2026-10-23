"""DM（主机端，大模型）：推进剧情的 NarratorAgent。

护栏从「事后校验 LLM 的 JSON」改成了「工具本身只在合法范围内生效」——
这是标准 Agent 写法：模型能做什么，由它拿到的工具决定。

- 只读工具：read_scene / lookup_script（当前场景的原文、出口、检定已经整段注入
  user 消息，所以**没有** list_exits / list_checks —— 那两次调用纯属重复劳动）
- 写工具（会改游戏状态）：roll_check / move_to / set_flag
  - move_to 只接受「当前场景列出的、且条件已满足的出口」，否则返回错误让模型重试
  - set_flag 只接受剧本声明过的 flag
  - roll_check 由服务端投骰，模型拿不到骰子，也就编不出结果
- finish：模型显式收尾

**没有模型就没有产出**（2026-10-13 用户拍板取消脚本化兜底）：不再退化成
`dm.py` 的确定性流水线去拼一段旁白冒充 DM，而是**直接失败** —— 界面会明说
这一轮没有生成。要玩就必须有一个模型（本地引擎或云 API）。
"""
import json
import re
from dataclasses import dataclass, field

from .. import character_card, dice
from ..dm import DM  # 只为 DM._exit_ok（出口门控判定）；不再用它的脚本化流水线
from .base import Agent, AgentResult, Tool, _obj, int_prop, str_prop


@dataclass
class NarratorContext:
    """一次 DM 行动所需的上下文（state 是权威状态，events/parts 是本轮产物）。"""

    state: object
    player_name: str = "冒险者"
    events: list = field(default_factory=list)          # 待广播的事件（dice/system）
    narration_parts: list = field(default_factory=list)  # 系统自己产出的权威旁白
    npc_brief: str = ""  # 本轮涉及 NPC 的「人物卡 + 状态快照 + 反应采样结论」（不含记忆流水账）
    user_action: str = ""  # 本轮玩家原话（工具要拿它做检定关键词匹配，模型传参不可信）
    oob_verb: str = ""   # 非空 = 玩家动作被判定为越界（剧本机制之外），见 out_of_bounds_verb
    kb_brief: str = ""   # 知识库命中（玩家话里出现库里的名字时，确定性注入，不靠模型调工具）
    world_present: str = ""  # 当前场景**真实存在**的东西（世界层）
    world_digest: str = ""   # 进度：第几轮/第几天/还没收束的线索/去过的地方
    world_unresolved: list = field(default_factory=list)  # 玩家提到、但现场/原文都没有的名词
    world_move: str = ""     # 非空 = 服务端已经把队伍带到这个地点（要你描写它）


NARRATOR_JSON_HINT = {
    "role": "user",
    "content": (
        "该模型无法使用工具。请只输出一个 JSON 对象，不要任何解释文字：\n"
        '{"narration":"2-4 句旁白","move_to":"场景id或null","set_flags":["flag或空数组"]}\n'
        "move_to 只能是当前场景已列出的出口；set_flags 只能使用提示中给出的合法 flag。"
    ),
}

# 「这句话是不是在宣称检定成功了」——用来兜住 4B 最常犯的那个错：
# 骰子判失败，旁白却写「石闩被缓缓抬起」。命中就丢掉模型这一段。
_SUCCESS_WORDS = (
    "成功", "发现", "拿到", "得手", "打开", "推开", "抬起", "破解",
    "找到", "取下", "识破", "看穿", "撬开", "砸开",
)
# 越界动作的「做成了」写法跟检定成功不一样——是这套动词自己的完成态。
# 「火堆燃了起来」「墙被挖开了」都不在通用表里，越界局要用这张表核对。
_OOB_SUCCESS_WORDS = (
    "点燃", "燃起", "燃了", "烧起来", "烧了起来", "生了", "烧旺",
    "挖开", "凿开", "砸穿", "撬下", "升起",
)
_NEGATIONS = ("没", "未", "无", "不", "别")

# 工具报错的特征串：这些是「给你看的」，念给玩家等于把系统的拒绝当剧情。
# 模型把**整段提示词**当旁白念了出来（实测第 4 轮把【故事圣经】【进度】全抄了一遍，
# 而且上一轮的同类输出已经进了 chronicle，又被下一轮注入——会自我放大）。
# "没有侦查检定 / 无法进行相关检定" 这类变体：4B 会改写错误信息，固定串抓不住
_TOOL_ERROR_RE = re.compile(r"(没有|不存在)[^。！？]{0,10}检定|无法进行[^。！？]{0,8}检定|没有可用的检定")

_PROMPT_ECHO = ("【当前场景】", "场景id=", "【剧情档案", "【故事圣经】", "【进度】", "玩家行动：")

_TOOL_ERROR_ECHO = (
    "没有检定", "没有这个检定", "无法进行相关检定", "请尝试其他动作",
    "不得投骰", "参数错误", "该工具", "可用的检定",
)


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（只保留上一行，O(min) 内存；两串都截到 400 字）。"""
    a, b = a[:400], b[:400]
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def asserts_success(text: str, words=_SUCCESS_WORDS) -> str:
    """文本里有没有「做到了」的断言；排除「未能发现」「空无一物」这类否定/中性写法。

    命中就返回那个词（用于日志与测试），没命中返回空串。
    只看词前 4 个字里有没有否定词 —— 中文的否定几乎都贴着动词。
    `words` 可换词表：越界动作核对时传 `_OOB_SUCCESS_WORDS`。
    """
    for word in words:
        i = text.find(word)
        while i >= 0:
            if not any(n in text[max(0, i - 4):i] for n in _NEGATIONS):
                return word
            i = text.find(word, i + 1)
    return ""


def echoes_text(text: str, source: str, probe: int = 40) -> bool:
    """旁白是不是把已经播报过的原文整段抄了一遍（取前 40 字做探针）。"""
    src = (source or "").strip()
    if len(src) < probe or not text:
        return False
    return src[:probe] in text


# ---- 越界动作：剧本机制之外的「改造世界」型动词 ----
# 生火、挖墙这类动作在剧本里没有对应机制（不是出口触发说法、也不挂任何检定），
# 提示词只写了「别顺着他演」，但 4B 的实测是照写成功（「火堆燃起来了」）。
# 与其靠模型自觉，不如**入口就判出来**：命中越界动词、且动作没碰到任何出口/检定
# 关键词 → 在 user 消息里插一条硬指令，事后核对时旁白若把它写成成功就丢掉。
_OOB_VERBS = ("生火", "点火", "挖", "凿", "砸墙", "撬墙", "拆墙", "砍墙", "烧掉")


def out_of_bounds_verb(text: str, scene: dict) -> str:
    """玩家动作是否越界。返回命中的越界动词（"" = 没越界）。

    判定两步走：先看动作有没有命中**任何**出口/检定的触发说法（命中说明剧本
    有机制接它，放行）；再匹配越界动词表。两边都不沾才判越界。
    """
    t = text or ""
    if not t:
        return ""
    for thing in scene.get("exits", []) + scene.get("checks", []):
        for k in thing.get("keywords", []) or []:
            if k in t:
                return ""
    for v in _OOB_VERBS:
        if v in t:
            return v
    return ""


# ---- 元问题：入口直接分流，不进模型 ----
# 「能存档吗」「你是 AI 吗」这类问题提示词里写了要答，实测 4B 仍然复读上一轮旁白
# —— 对小模型来说，指令排在几十轮上下文深处就是没有。这类问题**根本不需要模型**：
# 答案是确定的系统信息，入口用正则接住直接回，0 秒、0 token。
# 输入长度卡 40 字：跑团里说「我帮助卡特」不该被「帮助」误伤 —— 元问题都很短。
_META_PATTERNS = (
    (r"存档|保存进度|存进度|接着上次|下次还能",
     "（系统）本局进度保存在服务器内存中，服务器重启后不保留；掉线重连即可回到当前房间，不需要手动操作。"),
    (r"你是(不是)?(ai|人工智能|机器人|真人)|人工智能|大模型|语言模型",
     "（系统）是的，主持人由 AI 扮演：语言模型负责旁白与 NPC，骰子由服务器投掷，剧情严格按剧本推进。"),
    (r"^(帮助|help)[吗?？!！.。]*$|怎么玩|规则是什么|怎么投骰|骰子怎么|有什么命令",
     "（系统）直接用一句话描述你的行动即可，例如「我凑近看浮雕」。需要检定时服务器会自动投骰；"
     "想去哪里就直接说。拿不准做什么，点输入框旁的小助手。"),
)


def meta_answer(text: str) -> str:
    """玩家输入是不是元问题（规则/存档/身份）。是 → 返回固定答案；否 → 空串。"""
    t = (text or "").strip()
    if not t or len(t) > 40:
        return ""
    low = t.lower()
    for pat, ans in _META_PATTERNS:
        if re.search(pat, low):
            return ans
    return ""


class NarratorAgent(Agent):
    name = "narrator"
    max_steps = 6
    json_hint = NARRATOR_JSON_HINT

    def __init__(self, store, provider=None, npc=None, chronicle=None, kb=None, world=None,
                 rules=None):
        super().__init__(provider)
        self.store = store
        self.npc = npc  # NpcService（可空）；DM 只经它读人物卡，拿不到 NPC 的记忆流水账
        self.chronicle = chronicle  # Chronicle（可空）；DM 只经 recall_history 按需检索，改不了档案
        # 编译器产出的知识库（五类容器）。有就多两个只读工具，没有就不加 —— 游戏照跑。
        self.kb = kb
        # 判定引擎：按规则书的档案造骰子（COC=d100 掷低）。没档案就退回 d20。
        self.rules = rules
        # 这局的世界层（独立库）。**它是"现场存在什么"的唯一权威** ——
        # 没有它，DM 只能靠聊天记录猜，玩家说"我打开门"它就顺着演。
        self.world = world

    async def run(self, ctx, user_input: str = "") -> AgentResult:
        """跑一轮，并在返回前**核一遍旁白与权威结果是否打架**。

        两种打架都会把模型那一段丢掉（`drop_reason` 留痕，测试用它断言）：

        1. 本轮有失败的检定，旁白却写成成功 —— 实测 27% 的失败检定点会这样；
        2. 刚换场景，旁白把系统已经播报过的场景原文又抄一遍 —— 实测 24% 的推进轮。

        丢掉不等于「没产出」：权威文案（检定结果 / 新场景原文）本来就是系统自己广播的，
        玩家一个字都不会少。宁可少一句衔接，也不给一句自相矛盾的话。

        在进模型之前还有两道**确定性**分流（不花一次生成、不烧一个 token）：

        - 元问题（存档 / 你是 AI / 规则）：答案固定，入口直接回，不进工具循环。
          `source="system"`、`stopped="meta"`，界面不会把它当失败刷屏。
        - 越界动作（生火、挖墙这类剧本机制外的事）：记在 `ctx.oob_verb` 上，
          user 消息里追加硬指令（见 `build_user_message`），事后核对兜底（见 guard）。
        """
        ctx.user_action = user_input or ""
        meta = meta_answer(user_input)
        if meta:
            return AgentResult(
                text=meta,
                source="system",
                stopped="meta",
                data={"meta": True},
            )
        ctx.oob_verb = out_of_bounds_verb(ctx.user_action, ctx.state.current())
        before = ctx.state.current_scene
        res = await super().run(ctx, user_input)
        reason = self._guard_narration(ctx, res.text or "", before)
        if reason:
            # **任何**被护栏丢弃的旁白都重试一次，并把原因明确告诉模型——
            # 直接留一段空白对玩家更糟（实测第 3 轮因为"雷同"被丢，玩家什么都没看到）。
            hint = {
                "复述了工具的报错": "不要提「检定」二字，也不要复述任何工具报错。",
                "把上下文念了出来": "只写旁白本身，不要抄任何【】小节。",
                "与上一轮旁白高度雷同": "换一个具体的细节写，不要重复上一轮的画面。",
                "与之前某轮的旁白逐字重复": "换一个具体的细节写，不要重复之前的句子。",
                "把 DM 内幕念成了旁白": "DM 内幕不能说出口，只写玩家能看见/听见的。",
            }.get(reason, "换一种写法，直接给玩家一段新的旁白。")
            res2 = await super().run(ctx, user_input + "\n（提醒：" + hint + "）")
            reason2 = self._guard_narration(ctx, res2.text or "", before)
            if not reason2:
                res2.data = {**(res.data or {}), "retried": True}
                return res2
            res.drop_reason = reason2
            res.text = ""
            return res
        return res

    def _guard_narration(self, ctx, text: str, before_scene: str) -> str:
        if not (text or "").strip():
            return ""
        # 工具返回值里那句「系统已广播新场景原文，你只需补一句衔接」被 4B 原样念出来过
        # （`系统已广播新场景原文。甬道深处传来石门的低鸣…`）—— 玩家不该看见系统指令。
        if "系统已广播" in text or "你只需补一句" in text:
            return "把系统指令念成了旁白"
        # 工具报错被原样念给玩家 —— 实测玩家问「我现在在哪儿」，DM 把 roll_check 的
        # 报错「当前场景没有检定 侦查，无法进行相关检定」直接当旁白说了出去。
        if any(h in text for h in _TOOL_ERROR_ECHO) or _TOOL_ERROR_RE.search(text):
            return "复述了工具的报错"
        if any(h in text for h in _PROMPT_ECHO) or text.count("\n") > 8:
            return "把上下文念了出来"
        # **DM 内幕被念出来**：场景的 dm_notes 是底牌。实测 4B 把
        # 「这间石室没有门…绝对不要凭空给出一扇门」整段念给了玩家。
        notes = (ctx.state.current() or {}).get("dm_notes") or ""
        if notes:
            a = re.sub(r"\s+", "", notes)
            b = re.sub(r"\s+", "", text)
            # 12 字连续重合是"照抄"；但实测 4B 会加两个虚词改写（"码头方向确实有东西
            # 从水里上来"），所以再补一条：最长公共子串 ≥ 8 字也算泄漏。
            if _lcs_len(a, b) >= 8:
                return "把 DM 内幕念成了旁白"
        if any(e.get("type") == "dice" and not e.get("success") for e in ctx.events):
            word = asserts_success(text)
            if word:
                return f"检定失败却写成成功（{word}）"
        if ctx.state.current_scene != before_scene:
            if echoes_text(text, ctx.state.current().get("public_text", "")):
                return "复述了系统刚播报的场景原文"
        # 热层里存着最近几条旁白原文，模型会直接抄回来 —— 实测越界局里
        # 连续两轮一字不差，其中一轮骰子还是反的。逐字相同的旧旁白一律丢掉：
        # 这一轮本来也没有新信息，少一句好过给一句假话。
        if self.chronicle is not None:
            t = text.strip()
            for e in self.chronicle.entries[-4:]:
                if e.get("kind") == "narration" and (e.get("text") or "").strip() == t:
                    return "与之前某轮的旁白逐字重复"
            # 逐字相同抓不到"改写式复读"：实测第 3 轮把第 2 轮的画面整段搬了过来。
            # 用 4-gram 覆盖率判：新旁白里有多少片段在上一轮里出现过。
            last = next((e.get("text") or "" for e in reversed(self.chronicle.entries)
                         if e.get("kind") == "narration"), "")
            if last and _overlap_ratio(t, last) > 0.75 and len(t) > 40:
                return "与上一轮旁白高度雷同"
        # 越界动作写成成功：入口已经判过越界（ctx.oob_verb），旁白里又出现了
        # 那个动词本身 + 成功断言（「生火…火堆燃起来了」）。两个条件同时命中
        # 才丢 —— 单看成功词会误伤「你发现墙上有火把架」这类合法补充。
        oob = getattr(ctx, "oob_verb", "") or ""
        if oob and oob in text and asserts_success(text, _OOB_SUCCESS_WORDS):
            return f"越界动作（{oob}）被写成了成功"
        return ""

    # ---- 提示词 ----
    # 第一层：System prompt —— 只写「我是谁、我能做什么、我的边界」，与具体剧本无关。
    def system_prompt(self, ctx) -> str:
        kb_block = ""
        if self.kb is not None and getattr(self.kb, "available", False):
            kb_block = (
                "【规则书知识库（与剧本无关的通用资料）】\n"
                "- 玩家问某个生物/物品/技能的**具体数值或规则**（它有多强、这枪伤害多少、"
                "这个技能基础值多少、某条判定怎么做）→ 用 kb_lookup(名字)。\n"
                "- 玩家问**世界观/设定**（这是哪个神话、典籍里写了什么、有什么传说）→ 用 kb_lore(主题)。\n"
                "- 查到的数值**你自己用来裁定**；旁白里不主动报数字，"
                "但玩家明确问数据/规则（「伤害多少」「怎么判」）时可以简短报出并给出处。\n"
                "- 查不到就说「书上没有记载」，**绝不编数值**。\n"
            )
        world_block = ""
        if self.world is not None:
            world_block = (
                "【世界状态是权威】\n"
                "- 【现场】为空**不等于什么都没有**：场景原文里写到的物件照常存在。"
                "【现场】列出的是**当前真实存在**的东西。玩家提到的东西如果【现场】没有、"
                "规则书里也没有，就说明这里没有它 —— 用一句话说清（例如「这间石室里没有门」），"
                "**不要顺着玩家把它演出来**。\n"
                "- **动作的前提不成立时，第一句就说清楚**：玩家要开的门、要走的路根本不存在，"
                "就明确说「这里没有门 / 这条路不通」，再给出现在能做的事，不要用气氛描写绕过去。\n"
                "- 你旁白里**新出现**的物件/人物，必须先用 declare 登记；不登记，下一轮它就不存在，"
                "前后会自相矛盾。\n"
                "- 玩家发现新线索/接下委托/定下计划时，用 open_thread 记一条（还没了结的事）；"
                "事情了结或放弃时用 close_thread 收掉。\n"
                "- 怪物、法术、关键道具只能来自知识库（kb_lookup），不能自己发明；"
                "没有合适数值的东西，就不要让它出现。\n"
                "【例子：玩家断言了不存在的东西】\n"
                "1. 原文没写门，玩家说「我推开门」→「这里没有门。你摸到的只有潮湿的石壁。」\n"
                "2. 原文写着「石壁上挂着一盏没油的青铜灯」，玩家问「灯座下的刻字是什么」→"
                "灯**存在**、刻字**不存在**：「灯座上没有刻字，只有一层锈。」"
                "（原文写到的物件要承认，原文没写的细节不要补）\n"
                "3. 原文和周遭都没桌子，玩家说「我拿起桌上的钥匙」→「这里没有桌子，也没有钥匙。」\n"
                "**判断顺序：先看场景原文 → 再看【现场】；两处都没有，就是不存在。**\n"
                "- **但「去某个地方」不是断言、是移动**：沙盒模式下玩家说「去码头 / 回旅馆」，"
                "用 travel(place=…, description=…) 当场长出新地点，**不要回「这里没有码头」**；"
                "固定剧本模式才用 move_to。\n"
            )
        return (
            "你是一场中文文字跑团（TRPG）的主持人（DM）。\n"
            "【你的职责】\n"
            "1. 依据给定的剧本，为玩家行动给出 2-4 句简短、有画面感的旁白，推动剧情前进。\n"
            "2. 判定玩家意图：该投骰就投骰，该换场景就换场景，其余自由发挥。\n"
            "3. 扮演剧本中的 NPC，但不替玩家做决定。\n"
            "【当前场景已经全给你了】\n"
            "用户消息的【当前场景】里有：原文、DM 内幕、在场 NPC、"
            "**每个出口及其解锁状态**、**可触发的检定（id / 技能 / DC）**。\n"
            "这就是判断这一轮所需的全部信息 —— **不要再为了「确认一下」去调工具**。\n"
            "- 要换场景：move_to（出口见【当前场景】）。它的返回值会带上新场景的完整信息，不必再读。\n"
            "- 要投骰：roll_check（检定 id 见【当前场景】）。\n"
            "- 只有这几种才需要查：剧本别处的设定用 lookup_script；"
            "某个 NPC 的过往用 npc_card 或 recall_history；出场了剧本里没有的新 NPC 用 npc_introduce。\n"
            + kb_block
            + world_block
            + "- 剧情明确产生新线索时才 set_flag。\n"
            "【硬性边界】\n"
            "- 严格遵循给定的剧本，不得编造剧本之外的走向、NPC、物品或地点。\n"
            "- 骰子只能由 roll_check 产生，你不得编造任何骰子结果，也不要在旁白里报出数值。\n"
            "- **检定失败就写失败**：说「没做到、一无所获、看不出所以然」，"
            "绝不写门被打开、东西被拿到、线索被发现。\n"
            "- **新场景的原文系统已经播报了**：move_to 之后只补一句衔接，不要复述原文。\n"
            "- **剧本之外的动作不要顺着他演**：玩家要生火、挖墙、抢人东西、骂人、跳过剧情，"
            "就用一句话说明这里做不到（或没人理他），然后把选择权交回给玩家。\n"
            "- **玩家问规则或元问题**（能不能存档、你是不是 AI、这关怎么过），"
            "用一句话直接回答，不要当没听见，也不要借机推进剧情或投骰。\n"
            "- **玩家问「我在哪 / 这里是什么地方 / 周围有什么」**：用【当前场景】的地点与原文"
            "直接回答（一两个具体细节即可），**不要调 roll_check，也不要提「检定」二字**。\n"
            "- **旁白要有引导**：每段至少给一个可感知的细节（声音、气味、光影、 NPC 的小动作），"
            "再留一个能让人动手的钩子（一扇半掩的门、一句没说完的话、地上的一道拖痕）。"
            "不要只做判定或只回答一句话；也不要替玩家决定他做什么，更不要每轮都用"
            "「你们要怎么做」收尾。\n"
            "- **工具返回 error 时**：那是给你看的，直接用旁白回应玩家，"
            "**绝不要把错误信息写进旁白**（写了会被系统丢掉并重来）。\n"
            "- 剧本中标注为「DM 内幕」的内容是你的底牌，不能直接说出口。\n"
            "- move_to / roll_check 调用后系统会自行广播权威结果，你的最终旁白只补一句衔接（可以留空）。\n"
        )

    # 第二层：次级 prompt —— 剧本内容（世界观/语气/切片索引），随剧本切换而变。
    def secondary_prompt(self, ctx) -> str:
        return self.store.script_prompt

    def build_user_message(self, ctx, user_input: str) -> str:
        state = ctx.state
        cur = state.current()
        players = "；".join(
            character_card.player_line(p.name, p.character, getattr(self.store, "card_schema", None))
            for p in state.players.values()
        )
        npc_block = f"\n【在场 NPC（人物卡）】\n{ctx.npc_brief}" if getattr(ctx, "npc_brief", "") else ""
        hist = self.chronicle.digest(6) if self.chronicle is not None else ""
        hist_block = f"\n【剧情档案（热层）】\n{hist}\n（需要更早的细节时用 recall_history 检索）" if hist else ""
        oob = getattr(ctx, "oob_verb", "") or ""
        oob_block = (
            f"\n【系统判定：越界动作】\n"
            f"玩家想做的是「{oob}」这类事 —— 它不是任何出口的触发说法，也不对应任何检定，"
            "剧本没有给这个动作任何机制。旁白必须用一句话明确说明这里做不到（或没人理会），"
            "然后把选择权交回给玩家。**不得描写这个动作成功**。\n"
            if oob
            else ""
        )
        present = getattr(ctx, "world_present", "") or ""
        present_block = (f"\n【现场】\n{present}\n" if present else "")
        move = getattr(ctx, "world_move", "") or ""
        move_block = (
            f"\n【系统已经带队移动】玩家到了「{move}」。\n"
            "这里还**没有描写**：请用 2–3 句写它现在的样子（第二人称、具体感官），"
            "并用 declare 登记至少一个能看/能碰的东西。\n"
            if move
            else ""
        )
        unresolved = getattr(ctx, "world_unresolved", None) or []
        unresolved_block = (
            "\n【未落地的指代】玩家提到了：" + "、".join(f"「{n}」" for n in unresolved[:4])
            + " —— 它们不在现场，也不在场景原文里（这是服务端比对出来的，不是猜测）。\n"
            "  · 如果是玩家**凭空断言**的（刻字、钥匙、门），明确说没有，不要让它出现；\n"
            "  · 如果只是**布景**（桌子、灯、脚印）且你确实要写它，先 declare 再写。\n"
            if unresolved
            else ""
        )
        bible_block = ""
        if self.world is not None:
            try:
                raw = self.world.get("bible")
                if raw:
                    b = json.loads(raw)
                    bible_block = (
                        "\n【故事圣经】（本局常设，不是每轮现编）\n"
                        f"年代地点：{b.get('era')} · {b.get('place')}\n"
                        f"基调：{b.get('tone')}\n"
                        f"本局可能出现的威胁：{'、'.join(b.get('monsters') or []) or '（未定）'}\n"
                        f"禁忌：{'、'.join(b.get('forbidden') or []) or '无'}\n"
                        f"核心冲突：{b.get('premise')}\n"
                    )
            except Exception:  # noqa: BLE001
                bible_block = ""
        digest = getattr(ctx, "world_digest", "") or ""
        digest_block = (f"\n【进度】\n{digest}\n" if digest else "")
        kb = getattr(ctx, "kb_brief", "") or ""
        kb_block = (
            f"\n【知识库参考（规则书原文，用来裁定；不是剧本内容）】\n{kb}\n"
            "（上面的数值/条目照用，旁白里不必逐字念；书里没有的就按规则临场裁定。）"
            if kb
            else ""
        )
        return (
            f"{self.store.build_context(state.current_scene, user_input, flags=state.flags)}\n\n"
            f"当前场景：{state.current_scene} 地点：{cur.get('location', '')}\n"
            f"已获得 flag：{sorted(state.flags) or '无'}\n"
            f"在场玩家：{players}"
            f"{hist_block}"
            f"{bible_block}"
            f"{digest_block}"
            f"{present_block}"
            f"{unresolved_block}"
            f"{move_block}"
            f"{npc_block}"
            f"{kb_block}"
            f"{oob_block}\n"
            f"玩家行动：{user_input}"
        )

    # ---- 预热 ----
    async def warmup(self, ctx) -> bool:
        """用**真实长度**的 prompt 空转一次前向，把首次 prefill 的成本提前付掉。

        为什么值得：实测（4B Q4 / RTX 5060）DM 的第一次生成，2000 token 的 prompt 要
        26 秒；同样长度第二次只要 7–10 秒。那 26 秒是引擎按序列长度做首次 prefill /
        kernel 准备，不是模型本身慢。玩家进房之后本来就在等待室里等 —— 正好付在这里。

        **不给工具**这句话现在只对"执行"成立：为了让 prefill 的长度与真实一轮**一致**，
        这里仍然把工具 schema 发过去（它占 ~510 token，是 prompt 的大头之一），只是拿到
        的 `tool_calls` 一律丢弃 —— 没有 handler 会跑，状态改不了。
        `provider.needs_warmup` 让云 API / 本地服务直接跳过（首字延迟不在我们这边）。
        """
        if self.provider is None or not getattr(self.provider, "needs_warmup", False):
            return False
        msgs = [
            {"role": "system", "content": self.system_prompt(ctx)},
            {"role": "system", "content": self.secondary_prompt(ctx)},
            {"role": "user", "content": self.build_user_message(ctx, "（预热，请只回复「好」）")},
        ]
        # schema 发过去只为凑长度；返回值丢掉
        await self.provider.chat_with_tools(msgs, [t.schema() for t in self.build_tools(ctx)])
        return True

    # ---- 工具 ----
    # **只有 6 个**，而且刻意不含 list_exits / list_checks：两者要回答的问题
    # （这个场景能往哪走、有什么检定）在【当前场景】里已经写好了，出口甚至带着
    # 解锁状态。曾经实测一轮行动 6 次生成、55 秒，其中 3 次就是模型在「确认」这两件事
    # 和重读当前场景 —— 信息重复提供 + 提示词命令它去查，它当然照办。
    def build_tools(self, ctx) -> list:
        tools = [
            Tool(
                "read_scene",
                "读取**其他**场景的原文与内幕（当前场景已在上下文里，不要为了确认而重读）。只读。",
                _obj({"scene_id": str_prop("场景 id；省略则读当前场景")}),
                self._t_read_scene,
            ),
            Tool(
                "lookup_script",
                "在剧本里检索与关键词最相关的片段（RAG）。只读。",
                _obj({"query": str_prop("检索关键词"), "k": int_prop("返回条数", 3)}),
                self._t_lookup_script,
            ),
        ]
        if self.npc is not None:
            tools.extend(
                [
                    Tool(
                        "npc_card",
                        "查看某个 NPC 的人物卡与当前状态（不含他的历史流水账）。只读。",
                        _obj({"name": str_prop("NPC 名字或别称")}),
                        self._t_npc_card,
                    ),
                    Tool(
                        "npc_introduce",
                        "为剧本里没有的新 NPC 建档（会生成人物小传），让他后续行为保持一致。",
                        _obj(
                            {"name": str_prop("NPC 名字"), "role": str_prop("身份，可选")},
                            ["name"],
                        ),
                        self._t_npc_introduce,
                        read_only=False,
                    ),
                ]
            )
        if self.world is not None:
            if (self.store.data or {}).get("sandbox"):
                tools.append(
                    Tool(
                        "travel",
                        "去一个新地方（沙盒模式）。你给出地名和 1–2 句场景描写，系统会把它登记成新地点。"
                        "玩家说「去码头/回旅馆」时用它。",
                        _obj({
                            "place": str_prop("地名，如 码头 / 阿卡姆旅馆"),
                            "description": str_prop("1–2 句场景原文，第二人称，有感官细节"),
                        }, ["place"]),
                        self._t_travel,
                        read_only=False,
                    )
                )
            tools.extend([
                Tool(
                    "declare",
                    "把**你旁白里新出现**的物件/人物/线索登记进世界（不登记，下一轮它就不存在了）。"
                    "要写「这里有一张木桌」，就先 declare(name=木桌, kind=object)。",
                    _obj({
                        "name": str_prop("名字，如 木桌 / 守墓人 / 血字"),
                        "kind": str_prop("object / npc / creature / clue / door"),
                        "detail": str_prop("一句描述，可选"),
                    }, ["name"]),
                    self._t_declare,
                    read_only=False,
                ),
                Tool(
                    "lookup_world",
                    "查某个东西现在在不在、在哪（世界层）。玩家提到一个可能不存在的东西时先确认。只读。",
                    _obj({"name": str_prop("要查的名字，如 门 / 青铜灯 / 守墓人")}, ["name"]),
                    self._t_lookup_world,
                ),
                Tool(
                    "open_thread",
                    "记下一条**还没了结**的线索/目标（玩家发现新线索、接受委托、定下计划时）。"
                    "之后每轮都会在【进度】里提醒你它还没收束。",
                    _obj({"title": str_prop("一句话，如 找到失踪的船员")}, ["title"]),
                    self._t_open_thread,
                    read_only=False,
                ),
                Tool(
                    "close_thread",
                    "收束一条线索（已了结或已放弃）。用线索标题的关键字即可。",
                    _obj({"title": str_prop("线索标题关键字"), "status": str_prop("done / dropped")},
                         ["title"]),
                    self._t_close_thread,
                    read_only=False,
                ),
                Tool(
                    "advance_time",
                    "推进游戏内时间（分钟）。调查/搜查/赶路各要多久，你按规则书估；系统负责昼夜与天数。",
                    _obj({"minutes": int_prop("分钟数，1–240", 10),
                          "reason": str_prop("为什么花这些时间")}, ["minutes"]),
                    self._t_advance_time,
                ),
            ])
        if self.kb is not None and getattr(self.kb, "available", False):
            try:
                from ..kb_tools import build_tools as _kb_tools

                tools.extend(_kb_tools(self.kb))
            except Exception:  # noqa: BLE001  知识库工具坏了不该拖垮 DM
                pass
        if self.chronicle is not None:
            tools.append(
                Tool(
                    "recall_history",
                    "检索本局此前发生过的剧情（旁白原文 / 玩家行动 / 骰点结论），用来回忆关键角色与事件。只读。",
                    _obj(
                        {
                            "query": str_prop("检索关键词，如某个 NPC 的名字或一桩事件"),
                            "k": int_prop("返回条数", 3),
                            "actor": str_prop("只看与这个角色相关的记录，可选"),
                        }
                    ),
                    self._t_recall_history,
                )
            )
        tools.extend(
            [
                Tool(
                    "roll_check",
                    "让服务端投掷某个检定并应用结果（可能获得线索）。这是唯一产生骰子结果的途径。"
                    "只能对玩家的动作确实命中触发说法的检定投骰，不得把检定套在无关动作上。",
                    _obj(
                        {
                            "check_id": str_prop(
                                "检定 id，见【当前场景】的检定列表；"
                                "沙盒模式没有列表时，直接填技能名（如 侦查 / 聆听 / 图书馆使用）"),
                            "trigger": str_prop("玩家行动中命中该检定触发说法的原话片段"),
                            "difficulty": str_prop(
                                "难度：regular / hard（困难）/ extreme（极限）；省略=常规。"
                                "只有在玩家处境明显更难时才用 hard"),
                        },
                        ["check_id"],
                    ),
                    self._t_roll_check,
                    read_only=False,
                ),
                Tool(
                    "move_to",
                    "把队伍推进到某个出口指向的场景。仅当该出口存在且条件已满足时生效。",
                    _obj({"scene_id": str_prop("目标场景 id，见【当前场景】的出口")}, ["scene_id"]),
                    self._t_move_to,
                    read_only=False,
                ),
                Tool(
                    "set_flag",
                    "记录一条剧情线索（只允许剧本声明过的 flag）。",
                    _obj({"flag": str_prop("flag 名")}, ["flag"]),
                    self._t_set_flag,
                    read_only=False,
                ),
                Tool(
                    "finish",
                    "给出最终旁白并结束本轮。",
                    _obj({"narration": str_prop("2-4 句最终旁白")}, ["narration"]),
                    self._t_finish,
                ),
            ]
        )
        return tools

    def _t_travel(self, ctx, place="", description=""):
        """沙盒里当场长出一个地点，并登记进世界层（剧本模式不许凭空长）。"""
        if self.world is None:
            return {"error": "这一局没有世界层"}
        place = (place or "").strip()[:20]
        if not place:
            return {"error": "请给出地名"}
        row = self.world.con.execute(
            "SELECT * FROM scene WHERE place=? OR label=?", (place, place)).fetchone()
        if row:  # 去过的地方就回去，不重复建
            sid = row["id"]
            self.world.enter(sid)
            ctx.state.enter(sid)
            ctx.state.turn += 1
            ctx.events.append({"type": "system", "text": f"回到：{place}"})
            return {"ok": True, "scene_id": sid, "returned": True,
                    "scene": self.store.scene_brief(sid, flags=ctx.state.flags)}
        cur = ctx.state.current() or {}
        if cur.get("exits") and not (self.store.data or {}).get("sandbox"):
            return {"error": "本剧本是固定场景，请用 move_to（出口见【当前场景】）",
                    "available": [e["to"] for e in cur.get("exits") or []]}
        n = int(self.world.get("gen_count", "0") or 0) + 1
        self.world.set("gen_count", n)
        sid = f"gen{n}"
        text = (description or "").strip()[:600] or f"{place}。"
        scene = {
            "id": sid, "chapter": 1, "title": place, "location": place,
            "public_text": text, "dm_notes": "", "npcs": [], "keywords": [place],
            "flags_required": [], "flags_set": [], "checks": [], "exits": [],
        }
        self.store.scenes[sid] = scene
        self.world.add_scene(sid, place, place, text,
                             parent=ctx.state.current_scene or "", source="generated")
        self.world.enter(sid)
        ctx.state.enter(sid)
        ctx.state.turn += 1
        ctx.events.append({"type": "system", "text": f"进入：{place}"})
        ctx.narration_parts.append(text)
        return {"ok": True, "scene_id": sid, "place": place,
                "scene": self.store.scene_brief(sid, flags=ctx.state.flags),
                "note": "新地点已登记进世界层。系统已广播场景原文，你只需补一句衔接。"}

    def _t_declare(self, ctx, name="", kind="object", detail=""):
        if self.world is None:
            return {"error": "这一局没有世界层"}
        row = self.world.declare(name, kind=kind, detail=detail)
        if not row:
            return {"error": "登记失败：名字为空或没有当前场景"}
        return {"ok": True, "entity": {"name": row["name"], "kind": row["kind"],
                                       "detail": row["detail"]},
                "note": "已登记进世界层，之后每一轮它都在【现场】里。"}

    def _t_lookup_world(self, ctx, name=""):
        if self.world is None:
            return {"error": "这一局没有世界层"}
        hit = self.world.find(name)
        if not hit:
            return {"exists": False, "note": f"世界层里没有「{name}」。不要顺着玩家把它演出来。"}
        here = hit["scene_id"] == self.world.get("scene")
        return {"exists": True, "here": here, "name": hit["name"], "kind": hit["kind"],
                "detail": hit["detail"],
                "note": "当前场景里确实有它。" if here else "它存在，但不在当前场景。"}

    def _t_open_thread(self, ctx, title=""):
        if self.world is None:
            return {"error": "这一局没有世界层"}
        title = (title or "").strip()[:80]
        if not title:
            return {"error": "请给出线索标题"}
        tid = self.world.open_thread(title)
        return {"ok": True, "id": tid,
                "open_threads": [t["title"] for t in self.world.threads("open")]}

    def _t_close_thread(self, ctx, title="", status="done"):
        if self.world is None:
            return {"error": "这一局没有世界层"}
        row = self.world.close_thread(title, status)
        if not row:
            return {"error": f"没找到未收束的线索：{title}",
                    "open_threads": [t["title"] for t in self.world.threads("open")]}
        return {"ok": True, "closed": row["title"], "status": row["status"],
                "open_threads": [t["title"] for t in self.world.threads("open")]}

    def _t_advance_time(self, ctx, minutes=10, reason=""):
        if self.world is None:
            return {"error": "这一局没有世界层"}
        ev = self.world.advance(minutes)
        if ev:
            ctx.events.append(ev)
        return {"ok": True, "turn": self.world.get("turn"), "day": self.world.get("day"),
                "phase": self.world.get("phase"),
                "note": ev["text"] if ev else f"过去 {minutes} 分钟。"}

    def _t_npc_card(self, ctx, name=""):
        if self.npc is None:
            return {"error": "本局未启用 NPC 子系统"}
        card = self.npc.registry.resolve((name or "").strip())
        if not card:
            return {"error": f"没有名为「{name}」的 NPC", "known": [c["name"] for c in self.npc.index()]}
        return self.npc.card_public(card)

    async def _t_npc_introduce(self, ctx, name="", role=""):
        if self.npc is None:
            return {"error": "本局未启用 NPC 子系统"}
        card = await self.npc.ensure_card((name or "").strip(), role=(role or "").strip())
        if not card:
            return {"error": "无法为该 NPC 建档", "name": name}
        return {"ok": True, "id": card.id, "name": card.name, "role": card.role, "known": card.summary[:80]}

    def _t_read_scene(self, ctx, scene_id=""):
        sid = (scene_id or "").strip() or ctx.state.current_scene
        s = self.store.get_scene(sid)
        if not s:
            return {"error": f"未知场景：{sid}", "known": sorted(self.store.scenes)}
        return {
            "id": s["id"],
            "title": s["title"],
            "location": s["location"],
            "public_text": s["public_text"],
            "dm_notes": s.get("dm_notes", ""),
            "npcs": [{"name": n["name"], "role": n["role"]} for n in s.get("npcs", [])],
        }

    def _t_recall_history(self, ctx, query="", k=3, actor=""):
        if self.chronicle is None:
            return {"error": "本局未启用剧情档案"}
        hits = self.chronicle.search((query or "").strip(), k=k, actor=(actor or "").strip() or None)
        # 每条截 300 字：这是一次工具结果，会**留在 messages 里**跟着后续每一次生成
        # 重新 prefill（不像热层那样只回 6 条）。原来截 400 字 × 4 条 ≈ 1000 token。
        return {
            "hits": [
                {
                    "turn": h.get("turn"),
                    "kind": h.get("kind"),
                    "scene": h.get("scene"),
                    "text": (h.get("text") or "")[:300],
                }
                for h in hits
            ],
            "note": "以上是本局档案里的原文摘录，请据此叙述，不要编造没有记录的事。",
        }

    def _t_lookup_script(self, ctx, query="", k=3):
        k = max(1, min(int(k or 3), 6))
        ids = self.store.retrieve(ctx.state.current_scene, query or "", k=k)
        return {
            "scenes": [
                {"scene_id": sid, "title": self.store.scenes[sid]["title"],
                 "excerpt": self.store.scenes[sid]["public_text"][:200]}
                for sid in ids
            ]
        }

    def _skill_value(self, ctx, skill: str) -> tuple[int, str]:
        """这次检定用多少技能值。顺序：角色卡 → 规则书技能基础值 → 30（无源则说明）。"""
        actor = ctx.state.players.get(ctx.player_name)
        ch = (actor.character if actor else {}) or {}
        for key in ("skills", "技能"):
            block = ch.get(key)
            if isinstance(block, dict):
                v = block.get(skill)
                if isinstance(v, (int, float)) and v > 0:
                    return int(v), "角色卡"
        if self.kb is not None and getattr(self.kb, "available", False) and hasattr(self.kb, "skill_value"):
            v, src = self.kb.skill_value(skill)
            if v:
                return v, src
        return 30, "默认（书里没有这条技能）"

    def _t_roll_check(self, ctx, check_id="", trigger="", difficulty=""):
        check_id = (check_id or "").strip()
        scene = ctx.state.current()
        check = next((c for c in scene.get("checks", []) if c["id"] == check_id), None)
        if not check and (self.store.data or {}).get("sandbox"):
            # 沙盒没有预注册检定：**直接按技能名临场判**（skill 就是 check_id 的写法）。
            # 这正是规则书该发挥作用的地方：技能值来自书，成功率来自判定档案。
            value, source = self._skill_value(ctx, check_id)
            r = (self.rules.check(check_id, value, difficulty=difficulty or "regular")
                 if self.rules is not None else dice.roll_check(check_id, 12))
            ok = r["success"]
            ctx.events.append({
                "type": "dice", "player": ctx.player_name, "skill": check_id,
                "dice": r.get("dice") or "d20", "dc": r.get("target"), "roll": r.get("roll"),
                "target": r.get("target"), "level": r.get("level"),
                "skill_value": r.get("skill_value"), "skill_source": source,
                "mod": r.get("mod", 0), "total": r.get("total"),
                "attr": "", "success": ok, "flag": None,
            })
            return {"rolled": True, "success": ok,
                    "system": (self.rules.describe() if self.rules is not None else "d20"),
                    "note": ("成功。旁白可以写他做到了。" if ok else
                             "**失败**。旁白必须写成「没做到 / 一无所获」，不得写出只有成功才会出现的结果。")}
        if not check:
            return {
                "error": f"当前场景没有检定 {check_id}",
                "available": [c["id"] for c in scene.get("checks", [])],
            }
        # 关键词门：检定声明了自己的触发说法（keywords）时，玩家动作必须命中其一。
        # 实测单检定场景里 4B 会把任何动作都套上它 —— 跳舞、骂脏话、看墙根全变成
        # 「学识检定」。骰子一旦投出去就是权威结果，收不回来，所以这道门必须在
        # **投骰之前**把无关动作拦下，并把它该被什么话触发告诉模型。
        # 匹配范围 = 玩家原话（ctx.user_action）+ 模型补报的触发片段（trigger）；
        # 只信模型传参不可靠，所以原话永远参与匹配。
        kws = check.get("keywords") or []
        if kws:
            probe = f"{getattr(ctx, 'user_action', '') or ''} {trigger or ''}"
            hits = [k for k in kws if k in probe]
            if not hits:
                return {
                    "error": (
                        f"检定 {check_id}（{check['skill']}）与玩家动作不匹配："
                        "动作没有命中它的任何触发说法，不得投骰。"
                    ),
                    "triggered_by": kws,
                    "note": (
                        "要么确认玩家动作确实是在做这件事，换个贴切的说法重新调用；"
                        "要么不用骰子，直接用旁白回应玩家的动作。"
                    ),
                }
        # 属性加值：谁能做成这件事，取决于**行动的人**自己的角色卡。
        # 没填卡 / 这个技能没映射到属性 ⇒ 加值 0，等同于原来的纯 d20 vs DC。
        card = getattr(self.store, "card_schema", None)
        actor = ctx.state.players.get(ctx.player_name)
        mod, attr = character_card.modifier_for(card, (actor.character if actor else {}), check["skill"])
        if self.rules is not None and getattr(self.rules, "is_d100", False):
            value, source = self._skill_value(ctx, check["skill"])
            r = self.rules.check(check["skill"], value,
                                 difficulty=(difficulty or check.get("difficulty") or "regular"))
            r["skill_source"] = source
        else:
            r = dice.roll_check(check["skill"], check.get("dc", 10), mod=mod)
        ok = r["success"]
        if ok and check.get("success_flag"):
            ctx.state.set_flag(check["success_flag"])
        outcome = check["success"] if ok else check["fail"]
        ctx.events.append(
            {
                "type": "dice",
                "player": ctx.player_name,
                "skill": check["skill"],
                "dice": r.get("dice") or "d20",
                "dc": r.get("target"),
                "roll": r.get("roll"),
                "target": r.get("target"),
                "level": r.get("level"),
                "skill_value": r.get("skill_value"),
                "skill_source": r.get("skill_source"),
                "mod": r.get("mod", 0),
                "total": r.get("total"),
                "attr": attr,
                "success": ok,
                "flag": check.get("success_flag") if ok else None,
            }
        )
        ctx.narration_parts.append(outcome)
        return {
            "rolled": True,
            "success": ok,
            "system": (self.rules.describe() if self.rules is not None else "d20"),
            # 把加值的来历告诉 DM：它可以据此说「你的学识让你看出了门道」，
            # 但照旧**不要在旁白里报数字**（提示词里那条硬边界不变）。
            "ability": f"{attr} {mod:+d}" if attr else "",
            "flag_gained": check.get("success_flag") if ok else None,
            # 这一行是给 4B 看的**硬指令**。实测它会把失败当成功写：
            # 力量检定掷出 11 vs DC13（石闩纹丝不动），下一句却是「石闩被缓缓抬起」。
            # 光在 system prompt 里写「遵循结果」不够 —— 工具返回是每轮都在眼前的，
            # 提示词是几十轮前就沉到上下文底部的。
            "note": (
                "成功。旁白可以写他做到了。"
                if ok else
                "**失败**。旁白必须写成「没做到 / 一无所获 / 看不出所以然」，"
                "不得写出只有成功才会出现的结果（门被打开、东西被拿到、线索被发现）。"
            ),
        }

    def _t_move_to(self, ctx, scene_id=""):
        scene_id = (scene_id or "").strip()
        cur = ctx.state.current()
        ex = next((e for e in cur.get("exits", []) if e["to"] == scene_id), None)
        if not ex:
            return {
                "error": f"{scene_id} 不是当前场景的出口",
                "available": [e["to"] for e in cur.get("exits", [])],
            }
        if not DM._exit_ok(ex, ctx.state):
            return {
                "error": f"出口「{ex['label']}」条件未满足",
                "locked": True,
                "required_flag": ex.get("condition"),
            }
        target = self.store.get_scene(scene_id)
        ctx.state.enter(scene_id)
        if self.world is not None:
            self.world.add_scene(scene_id, target.get("title") or scene_id,
                                 target.get("location") or "", target.get("public_text") or "",
                                 parent=ctx.state.current_scene, source="script")
            self.world.enter(scene_id)
        ctx.state.turn += 1
        ctx.events.append({"type": "system", "text": f"进入：{target['title']}"})
        ctx.narration_parts.append(target.get("public_text", ""))
        return {
            "ok": True,
            "scene_id": scene_id,
            "title": target["title"],
            # 把新场景整段带回去：否则模型下一句一定是 read_scene，白烧一次生成
            # （实测这一跳 3–6 秒，而内容本来就在手里）。
            "scene": self.store.scene_brief(scene_id, flags=ctx.state.flags),
            "note": "系统已广播新场景原文。上面的 scene 就是新场景的全部信息，不用再 read_scene，只需补一句衔接（可以留空）。",
        }

    def _t_set_flag(self, ctx, flag=""):
        flag = (flag or "").strip()
        if flag not in self.store.flag_ids:
            return {"error": f"未知 flag：{flag}", "allowed": sorted(self.store.flag_ids)}
        ctx.state.set_flag(flag)
        return {"ok": True, "flag": flag, "label": self.store.flag_label(flag)}

    def _t_finish(self, ctx, narration=""):
        return {"narration": (narration or "").strip()}

    # ---- 降级：模型不支持工具调用 → 单轮 JSON 协议（与旧行为一致）----
    def parse_final(self, text, ctx):
        obj = _parse_json(text)
        if not obj:
            return None
        move_to = obj.get("move_to")
        if move_to:
            self._t_move_to(ctx, move_to)
        for f in obj.get("set_flags") or []:
            if f in self.store.flag_ids:
                ctx.state.set_flag(f)
        return obj

    # 这里原本有一个 `run()` override：没有 provider 时走 `dm.py` 的确定性流水线，
    # 用剧本原文拼一段旁白冒充 DM。2026-10-13 用户拍板取消脚本化兜底后它被删掉了，
    # 行为完全等于基类：provider is None → source="failed" / stopped="no_provider"。


def _parse_json(raw):
    import json
    import re

    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {}
        try:
            val = json.loads(m.group(0))
            return val if isinstance(val, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


def _overlap_ratio(a: str, b: str, n: int = 4) -> float:
    """a 的 n-gram 有多少比例在 b 里出现过（改写式复读的判据）。"""
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if len(a) < n or len(b) < n:
        return 0.0
    grams = [a[i:i + n] for i in range(0, len(a) - n + 1)]
    hit = sum(1 for g in grams if g in b)
    return hit / max(1, len(grams))
