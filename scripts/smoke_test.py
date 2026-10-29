"""冒烟测试：验证 DM + RAG + 状态机 + 骰子 + 多人联机全链路。

用法：
  python scripts/smoke_test.py            # 进程内校验 + 实时联机校验（需先启动服务器）
  python scripts/smoke_test.py --unit     # 仅进程内校验

实时联机校验会启动两个 WebSocket 客户端加入同一房间，验证：
加入广播、行动→旁白、骰子事件、场景推进、助手建议。
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 控制台编码：Windows 上 Python 默认按系统代码页编码 stdout，**英文版是 cp1252**，
# 连"进程内校验"这几个字都印不出来 —— 而且是在第一行 print 就抛 UnicodeEncodeError，
# 一个断言都跑不到（CI 的 windows runner 就是这么红的）。显式转成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 被重定向到不支持 reconfigure 的对象
        pass

from server import __version__
from server.agents import (
    AdvisorAgent,
    AdvisorContext,
    AssistantAgent,
    NarratorAgent,
    NarratorContext,
    SummaryCtx,
    Task,
    ToolAgent,
    analyze_character_card,
    describe_agents,
)
from server import dice
from server.chronicle import Chronicle
from server.dice import seed as dice_seed
from server.dm import DM
from server.embedding import Embedder
from server.llm import EngineProvider, ToolCallingUnsupported, build_provider
from server.npc import ARCHETYPES, NpcService, PersonaCard, infer_archetype
from server.rag import ScriptStore
from server.script_loader import MAX_INDEX_CHUNKS, build_script_prompt, parse_script
from server.state_machine import GameState
from server.action_validation import validate as validate_action
from server.agents.action_judge import ActionJudgement

SCRIPT = Path(__file__).resolve().parent / "sample_script.json"
TOTSK = Path(__file__).resolve().parent / "totsk_l1.json"
PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


async def run_unit():
    print("== 进程内校验 ==")
    store = ScriptStore(SCRIPT)
    check("剧本加载（5 场景）", len(store.scenes) == 5, f"实际 {len(store.scenes)}")

    # 版本机制：__version__ 必须与 CHANGELOG 最新版本一致
    changelog = (SCRIPT.parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
    check(
        "版本号与 CHANGELOG 一致",
        bool(m) and m.group(1) == __version__,
        f"CHANGELOG={m.group(1) if m else '未找到'} 代码={__version__}",
    )

    # RAG 检索：搜"钥匙"应命中内院
    related = store.retrieve("gate", "我想找后门钥匙")
    check("RAG 检索命中相关场景", "courtyard" in related, str(related))

    # 状态机：flag 门控
    state = GameState(store, "gate")
    state.add_player("A", {"name": "A", "cls": "战士"})
    check("初始场景为 gate", state.current_scene == "gate")
    state.set_flag("found_secret_door")
    ok = state.enter("secret_room")
    check("flag 满足后可进入密室", ok and state.current_scene == "secret_room")

    # 出口条件门控：密室的进入限制挂在「出口」上，而非场景 flags_required
    dm_gate = DM(store, None)
    hall = store.get_scene("hall")
    state2 = GameState(store, "hall")
    state2.add_player("A", {"name": "A", "cls": "战士"})
    e_locked = dm_gate.detect_exit("打开暗门进入密室", hall)
    check("密室出口被正确识别", e_locked is not None and e_locked["to"] == "secret_room")
    check("无 flag 时出口被锁", not DM._exit_ok(e_locked, state2))
    state2.set_flag("found_secret_door")
    check("获得 flag 后出口解锁", DM._exit_ok(e_locked, state2))

    # 泛化词不误配：在门厅说「打开暗门进入密室」不应匹配到「木门→内院」
    e_bad = dm_gate.detect_exit("打开暗门进入密室", hall)
    check("泛化词不误配到内院", e_bad is None or e_bad["to"] != "courtyard", str(e_bad))

    # 骰子可复现
    dice_seed(123)
    r1 = __import__("server.dice", fromlist=["roll"]).roll(20)
    dice_seed(123)
    r2 = __import__("server.dice", fromlist=["roll"]).roll(20)
    check("骰子设种子可复现", r1 == r2, f"{r1} vs {r2}")

    # DM 的状态机实现：行动 → 检定 / 出口（`dm.py` 的确定性流水线）
    #
    # ⚠️ 2026-10-13 取消脚本化兜底后，这条流水线**不再是运行路径**：接了模型时
    # 由 DM 调 `roll_check` / `move_to` 决定走哪个检定与出口，没模型时直接失败。
    # 这里继续测它是为了守住"行动↔检定/出口 的匹配语义"这一层行为定义。
    dm = DM(store, None)
    state3 = GameState(store, "gate")
    state3.add_player("A", {"name": "A", "cls": "战士"})
    evs = await dm.handle_action(state3, "A", "说服老周")
    types = [e["type"] for e in evs]
    check("检定行动触发 dice 事件", "dice" in types, str(types))
    evs2 = await dm.handle_action(state3, "A", "推开铁门进入门厅")
    check("出口行动推进场景", state3.current_scene == "hall", state3.current_scene)

    # ---- 中途加入：进度判定 / 剧情回顾 / DM 引导 ----
    store2 = ScriptStore(SCRIPT)
    check("剧本驱动 flag 全集", {"has_key", "found_secret_door"} <= store2.flag_ids, str(sorted(store2.flag_ids)))
    check("flag 有中文描述", store2.flag_label("has_key") != "has_key", store2.flag_label("has_key"))

    fresh = GameState(store2, "gate")
    check("未开局的房间不算中途加入", not fresh.is_in_progress())
    fresh.add_player("A", {"cls": "战士"})
    check("首个玩家 joined_at_turn=0", fresh.players["A"].joined_at_turn == 0, str(fresh.players["A"].joined_at_turn))

    prog = GameState(store2, "gate")
    prog.add_player("A", {"cls": "战士"})
    await dm.handle_action(prog, "A", "推开铁门进入门厅")
    check("推进后判定为已开局", prog.is_in_progress())
    check("行程记录为 古堡大门→门厅", prog.scene_path() == ["古堡大门", "门厅"], str(prog.scene_path()))
    prog.set_flag("found_secret_door")
    r = prog.recap()
    check("回顾含中文线索", any(f["label"] != f["id"] for f in r["flags"]), str(r["flags"]))

    # 取消脚本化兜底（2026-10-13）：没有模型就不产这段「新玩家登场」旁白。
    # 有模型时由 `introduce()` 调模型生成（实时联机部分覆盖）。
    intro = await dm.introduce(prog, "梅林", {"cls": "法师"})
    check("无模型：中途加入不产引导旁白", not (intro or "").strip(), repr(intro))

    # ---- 房间级剧本同步：连入即拿到「剧本 + 两份 Agent 次级 prompt」----
    from server.main import Room

    room = Room("TEST1", store2)
    room.owner = "阿甲"
    host_ctx = room.room_context("阿甲")
    other_ctx = room.room_context("阿乙")
    check("房间：同步包带剧本信息", host_ctx["script_info"]["title"] == store2.title, str(host_ctx["script_info"]))
    check("房间：同步包带切片清单", len(host_ctx["chunks"]) == len(store2.chunk_list), str(len(host_ctx["chunks"])))
    check("房间：房主拿到 DM prompt", bool(host_ctx["prompt"]["dm"]))
    check("房间：非房主拿不到 DM prompt", other_ctx["prompt"]["dm"] is None, str(other_ctx["prompt"]["dm"])[:40])
    check("房间：所有人都拿到小助手 prompt", bool(other_ctx["prompt"]["advisor"]))
    check("房间：DM 与小助手 prompt 不相同", host_ctx["prompt"]["dm"] != host_ctx["prompt"]["advisor"])

    # ---- 本局随机种子：建房时掷一次、整局固定（引擎是纯函数，不给种子则每局雷同）----
    from server.engine import derive_seed

    check("房间：建房时掷出本局种子（63 位无符号）",
          isinstance(room.seed, int) and 0 <= room.seed < 2 ** 63, str(room.seed))
    check("房间：DM 与小助手各得一条独立随机流",
          derive_seed(room.seed, "dm") != derive_seed(room.seed, "assistant"))
    check("房间：同种子同标签可复现（日志里那个数能还原同一局）",
          derive_seed(room.seed, "dm") == derive_seed(room.seed, "dm"))


async def run_unit_totsk():
    """第二个剧本《蛇王墓·第一层》：验证真实模组（多场景/门控/机关）也能被引擎驱动。"""
    print("== 进程内校验：蛇王墓（改编自 Tomb of the Serpent Kings） ==")
    from server import dice as dice_mod

    store = ScriptStore(TOTSK)
    check("蛇王墓剧本加载（9 场景）", len(store.scenes) == 9, f"实际 {len(store.scenes)}")
    check("蛇王墓 flag 全集（12 个）", len(store.flag_ids) == 12, str(sorted(store.flag_ids)))

    # 版权归属必须留在文件里（改编自 CC BY-NC-SA 作品，署名是许可要求）
    meta = store.data.get("meta", {})
    check("剧本保留原作署名与许可", "Skerples" in json.dumps(meta, ensure_ascii=False), str(meta)[:80])
    check("剧本标注 CC BY-NC-SA", "BY-NC-SA" in meta.get("license", ""), meta.get("license", ""))

    # 结构完整性：出口指向存在、门控 flag 都在 flag 表、flag 都能拿到
    ids = {s["id"] for s in store.data["scenes"]}
    dangling = [
        f"{s['id']}->{e['to']}"
        for s in store.data["scenes"]
        for e in s.get("exits", [])
        if e["to"] not in ids
    ]
    check("出口无断链", not dangling, str(dangling))
    unknown_cond = [
        f"{s['id']}:{e['condition']}"
        for s in store.data["scenes"]
        for e in s.get("exits", [])
        if e.get("condition") and e["condition"] not in store.flag_ids
    ]
    check("出口门控 flag 都在 flag 表", not unknown_cond, str(unknown_cond))

    # RAG：搜"银戒指"应命中术士墓
    related = store.retrieve("entrance_hall", "我想去拿那枚银戒指")
    check("RAG 检索命中术士墓", "sorcerer_tomb" in related, str(related))

    # 检定 vs 出口 的优先级回归：这句必须判成「出口」，不能被子串命中抢成检定
    dm = DM(store, None)
    fk = store.get_scene("false_king_tomb")
    check(
        "退出措辞优先判为出口而非检定",
        dm.detect_check("侧身钻进棺后的窄缝", fk) is None,
        str(dm.detect_check("侧身钻进棺后的窄缝", fk)),
    )
    e_seam = dm.detect_exit("侧身钻进棺后的窄缝", fk)
    check("窄缝出口被识别", e_seam is not None and e_seam["to"] == "false_temple", str(e_seam))

    # 出口门控：石门未抬闩时进不去假王之墓
    sd = store.get_scene("stone_door")
    s_locked = GameState(store, "stone_door")
    s_locked.add_player("A", {"cls": "战士"})
    e_door = dm.detect_exit("推开石门进入假王之墓", sd)
    check("石门出口被识别", e_door is not None and e_door["to"] == "false_king_tomb", str(e_door))
    check("未抬闩时石门锁着", not DM._exit_ok(e_door, s_locked))
    s_locked.set_flag("opened_false_king_tomb")
    check("抬开石闩后石门解锁", DM._exit_ok(e_door, s_locked))

    # 检定关键词匹配：三种对待中空石像的方式各走各的检定
    gt = store.get_scene("guard_tomb")
    for action, want in [
        ("敲一敲石像", "listen_statue"),
        ("用长杆挑开石像", "probe_statue"),
        ("砸开石像", "smash_statue"),
    ]:
        c = dm.detect_check(action, gt)
        check(f"「{action}」→ {want}", c is not None and c["id"] == want, str(c and c["id"]))

    # 完整通关：把检定固定为成功，从开场一路走到第二层入口
    real_roll = dice_mod.roll_check

    def _always_success(skill, dc, mod=0):
        """钉死骰面 20 的假骰子。

        签名要跟真的 `roll_check` 一致（角色卡上线后多了 `mod`），返回的字段也一样 ——
        否则被替换掉的函数会在下游读 `r["mod"]` 时炸掉，而不是在"该失败的地方"失败。
        """
        bonus = int(mod or 0)
        return {"skill": skill, "dc": dc, "roll": 20, "mod": bonus,
                "total": 20 + bonus, "success": True}

    dice_mod.roll_check = _always_success
    try:
        run = GameState(store, store.start_scene)
        run.add_player("A", {"cls": "战士"})
        path = [
            "询问卡特",
            "钻进石门，走进甬道",
            "观察甬道",
            "进入守卫墓室",
            "敲一敲石像",
            "用长杆挑开石像",
            "返回甬道",
            "进入学者墓室",
            "查看卷轴",
            "返回甬道",
            "进入术士墓室",
            "取下戒指",
            "返回甬道",
            "走向尽头的石门",
        ]
        for act in path:
            await dm.handle_action(run, "A", act)
        check("通关前半程到达石门", run.current_scene == "stone_door", run.current_scene)

        # 未抬闩时推门应被拦下，且不改变场景
        evs = await dm.handle_action(run, "A", "推开石门进入假王之墓")
        blocked = any(e["type"] == "narration" and "做不到" in e.get("text", "") for e in evs)
        check("未抬闩时推门被拦下", blocked and run.current_scene == "stone_door", str([e["type"] for e in evs]))

        for act in [
            "检查石门",
            "合力抬开石闩",
            "推开石门进入假王之墓",
            "贴着棺盖听",
            "迎战骷髅",
            "检查北墙",
            "侧身钻进棺后的窄缝",
            "查看神像基座",
            "钻进秘道",
        ]:
            await dm.handle_action(run, "A", act)
        check("通关全程抵达第二层入口", run.current_scene == "upper_tomb", run.current_scene)

        want_flags = set(store.flag_ids)
        check("通关后集齐全部 12 条线索", run.flags >= want_flags, str(sorted(run.flags)))
        rec = run.recap()
        check("回顾行程覆盖 7 站以上", len(rec["scene_path"]) >= 7, str(rec["scene_path"]))
        check("回顾线索均为中文描述", all(f["label"] != f["id"] for f in rec["flags"]), str(rec["flags"])[:120])

        intro = await dm.introduce(run, "latecomer", {"cls": "法师"})
        check("蛇王墓：无模型的中途加入不产旁白（取消脚本化兜底）",
              not (intro or "").strip(), repr(intro))
    finally:
        dice_mod.roll_check = real_roll


async def run_unit_scripts():
    """剧本读取 / 自动切片 / 次级 prompt：验证「System 管职责、次级 prompt 管内容」的分层。"""
    print("== 进程内校验：剧本读取、切片与次级 prompt 分层 ==")

    # --- 1. 结构化剧本：切片 = 剧本设定 + 每个场景一个 ---
    st = ScriptStore(TOTSK, label="totsk_l1.json")
    check("切片数 = 1 个设定 + 9 个场景", len(st.chunk_list) == 10, str(len(st.chunk_list)))
    check("首个切片是剧本设定", st.chunk_list[0].kind == "meta" and st.chunk_list[0].id == "__meta__",
          f"{st.chunk_list[0].kind}/{st.chunk_list[0].id}")
    check("场景切片 id 与场景 id 一致",
          {c.id for c in st.chunk_list if c.kind == "scene"} == set(st.scenes),
          str(sorted(c.id for c in st.chunk_list if c.kind == "scene")))
    check("设定切片包含剧本原文（世界观/语气）", st.chunk_list[0].text.strip() == st.system.strip(), "")
    check("场景切片带关键词（供 RAG 与索引展示）",
          any(c.keywords for c in st.chunk_list if c.kind == "scene"), "")
    check("结构化剧本无自检告警", st.warnings == [], str(st.warnings))

    # --- 2. 次级 prompt（DM）：完整版 ---
    p_dm = st.script_prompt
    check("次级 prompt 含剧本名", st.title in p_dm, p_dm[:60])
    check("次级 prompt 含世界观/语气原文", "语气指南" in p_dm, p_dm[:200])
    check("次级 prompt 含切片索引", "【切片索引】" in p_dm and "tomb_mouth" in p_dm, p_dm[-260:])
    check("次级 prompt 只列索引、不展开正文（省 token）", "【DM 内幕】" not in p_dm, "")
    check("次级 prompt 用相对路径而非本机绝对路径",
          "totsk_l1.json" in p_dm and "E:\\" not in p_dm and "E:/" not in p_dm, p_dm[:120])

    # --- 3. 次级 prompt（小助手）：公开版，不给索引与内幕 ---
    p_adv = st.script_prompt_public
    check("小助手次级 prompt 含剧本名与背景", st.title in p_adv and st.premise[:8] in p_adv, p_adv[:120])
    check("小助手次级 prompt 不含场景索引（防剧透）",
          not any(sid in p_adv for sid in st.scenes), p_adv)
    check("小助手次级 prompt 不含 DM 内幕",
          "DM 内幕" not in p_adv and "DM内幕" not in p_adv, "")

    # --- 4. System prompt 只管职责：不夹带剧本内容 ---
    state = GameState(st, st.start_scene)
    state.add_player("A", {"cls": "战士"})
    n_agent = NarratorAgent(st, None)
    a_agent = AdvisorAgent(st, None)
    n_sys = n_agent.system_prompt(NarratorContext(state=state, player_name="A"))
    a_sys = a_agent.system_prompt(AdvisorContext(state=state, player_name="A"))
    check("DM 的 System prompt 是职责说明（含「你的职责」）", "你的职责" in n_sys, n_sys[:60])
    check("DM 的 System prompt 不含剧本正文", "语气指南" not in n_sys and "根窖" not in n_sys, "")
    check("DM 的 System prompt 不含剧本标题", st.title not in n_sys, "")
    check("DM 的 System prompt 不含场景 id",
          not any(sid in n_sys for sid in st.scenes), "")
    check("小助手 System prompt 是职责说明且声明不是主持人", "你的职责" in a_sys and "不是主持人" in a_sys, a_sys[:60])
    check("小助手 System prompt 不含剧本内容", st.title not in a_sys and "语气指南" not in a_sys, "")

    # --- 5. 消息分层：System(职责) → System(次级 prompt) → User ---
    msgs = n_agent.init_messages(NarratorContext(state=state, player_name="A"), "观察四周")
    check("消息三层结构与次序正确",
          [m["role"] for m in msgs] == ["system", "system", "user"],
          str([m["role"] for m in msgs]))
    check("第 1 条 = 职责", msgs[0]["content"] == n_sys, "")
    check("第 2 条 = 剧本次级 prompt", msgs[1]["content"] == st.script_prompt, "")
    check("第 3 条 = 本轮上下文（含场景与行动）",
          "当前场景" in msgs[2]["content"] and "观察四周" in msgs[2]["content"], msgs[2]["content"][:80])

    # 次级 prompt 真的被送进模型（而不是只在代码里生成）
    fake = FakeProvider([_reply(tool_call("finish", {"narration": "好的。"}))])
    await n_agent.__class__(st, fake).run(NarratorContext(state=state, player_name="A"), "看看")
    sent = fake.seen[0][1]
    check("实际请求里带了第二条 system 消息",
          len([m for m in sent if m.get("role") == "system"]) == 2,
          str([m.get("role") for m in sent]))
    check("实际请求里的次级 prompt 与生成的一致", sent[1]["content"] == st.script_prompt, "")

    # --- 6. 确定性流水线（无模型路径）也分两层 ---
    dm_msgs = DM(st, None)._build_messages(state, "（上下文）", "推门进去")
    check("DM 流水线消息同样两层 system",
          [m["role"] for m in dm_msgs] == ["system", "system", "user"],
          str([m["role"] for m in dm_msgs]))

    # --- 6.5 上下文瘦身：每轮都要重发的东西，一点冗余都不留 ---
    # 实测（RTX 5060 / 4B Q4）：一轮 6 次生成 / 55 秒，其中 3 次是模型在「确认」
    # 当前场景的出口与检定 —— 而这两样本来就写在 user 消息里了。下面把这件事钉住。
    ctx0 = NarratorContext(state=state, player_name="A")
    tool_names = [t.name for t in n_agent.build_tools(ctx0)]
    check("DM 工具不含 list_exits / list_checks（信息已在【当前场景】里）",
          "list_exits" not in tool_names and "list_checks" not in tool_names, str(tool_names))
    check("无 NPC/档案时 DM 只有这 6 件工具",
          set(tool_names) == {"read_scene", "lookup_script", "roll_check", "move_to", "set_flag", "finish"},
          str(sorted(tool_names)))

    # 出口的解锁状态直接算进【当前场景】，省掉 list_exits 那次调用
    brief = st.scene_brief(st.start_scene)
    check("场景简报自带出口与检定清单",
          ("出口：" in brief or "可触发的检定：" in brief), brief[-160:])
    cond = next(
        (e.get("condition") for s in st.scenes.values() for e in s.get("exits", []) if e.get("condition")),
        None,
    )
    if cond:
        sid = next(
            sid for sid, s in st.scenes.items()
            if any(e.get("condition") == cond for e in s.get("exits", []))
        )
        check("未拿到 flag 时出口标「需 xxx」", f"需 {cond}" in st.scene_brief(sid), st.scene_brief(sid))
        check("拿到 flag 后同一出口标「可用」",
              f"需 {cond}" not in st.scene_brief(sid, flags={cond}), st.scene_brief(sid, flags={cond}))
        check("不传 flags 时也要标出前置条件（不知道状态就别装作能走）",
              f"需 {cond}" in st.scene_brief(sid, flags=None), "")

    # build_context 把 flags 透下去（否则上面那套等于白算）
    ctx_txt = st.build_context(st.start_scene, "看看四周", flags=state.flags)
    check("build_context 把 flag 状态透传给场景简报", "可用" in ctx_txt, ctx_txt[:80])

    # move_to 把新场景整段带回去 —— 不然模型下一句一定是 read_scene，白烧一次生成
    ready = [e["to"] for e in state.current().get("exits", []) if DM._exit_ok(e, state)]
    if ready:
        r = n_agent._t_move_to(NarratorContext(state=GameState(st, st.start_scene), player_name="A"), ready[0])
        check("move_to 的返回值带新场景简报", "scene" in r and "公开文本" in (r.get("scene") or ""),
              str(r)[:140])

    # 切片索引封顶：大剧本不再把上下文吃光
    big_doc = {
        "title": "大本压测",
        "system": "世界观。" * 20,
        "start_scene": "s000",
        "scenes": [
            {
                "id": f"s{i:03d}", "title": f"第{i}间", "location": "地宫",
                "public_text": "一段场景描写。" * 4,
                "exits": ([{"to": f"s{i + 1:03d}", "label": "向前", "condition": None}]
                          if i < 59 else []),
                "checks": [],
            }
            for i in range(60)
        ],
    }
    st_big = ScriptStore.from_content(json.dumps(big_doc, ensure_ascii=False), "big.json")
    p_big = st_big.script_prompt
    check(f"大剧本的切片索引封顶在 {MAX_INDEX_CHUNKS} 行",
          p_big.count("\n- ") == MAX_INDEX_CHUNKS, str(p_big.count("\n- ")))
    check("折叠后给出「按关键词检索」的出口", "未列出" in p_big, p_big[-160:])
    check("索引行不再列字数与关键词（那是给人挑切片用的）",
          " 字" not in p_big and "关键词：" not in p_big, p_big[-200:])

    # --- 7. 无结构文本：自动切段 + 线性场景链 ---
    md = (
        "# 第一章 迷雾渡口\n夜色里渡船靠岸，船夫说河对岸有座白塔。\n\n"
        "# 第二章 白塔\n塔里没有楼梯，只有一圈又一圈向上盘绕的坡道。\n\n"
        "## 塔顶\n塔顶立着一只铜鸟，鸟嘴里衔着一枚钥匙。\n"
    )
    doc = parse_script(md, source="<inline>", name="雾港塔")
    check("文本剧本按标题切成 3 片", len(doc.chunks) == 3, str([c.title for c in doc.chunks]))
    check("文本剧本识别为纯文本格式", doc.fmt == "text" and not doc.structured, doc.fmt)
    check("文本切片 id 形如 c01/c02", [c.id for c in doc.chunks] == ["c01", "c02", "c03"], str([c.id for c in doc.chunks]))
    check("文本剧本自动合成线性场景链",
          [s["id"] for s in doc.data["scenes"]] == ["c01", "c02", "c03"], str(doc.data["scenes"]))
    check("线性链：每片指向下一片",
          doc.data["scenes"][0]["exits"][0]["to"] == "c02"
          and doc.data["scenes"][1]["exits"][0]["to"] == "c03"
          and doc.data["scenes"][2]["exits"] == [],
          str([s["exits"] for s in doc.data["scenes"]]))
    check("文本剧本开局 = 第一片", doc.start_scene == "c01", doc.start_scene)
    check("文本剧本给出降级提示", any("纯文本" in w or "无结构" in w for w in doc.warnings), str(doc.warnings))

    # --- 8. 长文本会被装箱成多片 ---
    long_text = "# 长章节\n" + "\n\n".join("这是第 %d 段。" % i + "填充" * 60 for i in range(12))
    doc2 = parse_script(long_text, name="长文")
    check("超长章节被切成多片", len(doc2.chunks) > 1, str([c.chars for c in doc2.chunks]))
    check("每片都不超过上限的 1.5 倍",
          all(c.chars <= 900 * 1.5 for c in doc2.chunks), str([c.chars for c in doc2.chunks]))

    # --- 9. 无结构文本也能真跑起来（状态机层仍可确定性推进，但 DM 侧不产旁白）---
    st_txt = ScriptStore.from_content(md, name="雾港塔")
    check("文本剧本也能建 ScriptStore", st_txt.fmt == "text" and st_txt.start_scene == "c01", st_txt.start_scene)
    s_txt = GameState(st_txt, st_txt.start_scene)
    s_txt.add_player("A", {"cls": "游侠"})
    txt_dm = DM(st_txt, None)
    evs = await txt_dm.handle_action(s_txt, "A", "继续前进")
    check("文本剧本可用确定性流水线推进", s_txt.current_scene == "c02", s_txt.current_scene)
    check("推进产出了旁白事件", any(e.get("type") == "narration" for e in evs), str(evs)[:80])

    # --- 10. 剧本自检：断链 / 不可达 / 缺 start_scene 都会被报出来 ---
    bad = json.dumps(
        {
            "title": "坏剧本",
            "start_scene": "nope",
            "scenes": [
                {"id": "a", "title": "A", "exits": [{"to": "ghost", "label": "去幽灵"}]},
                {"id": "b", "title": "B"},
            ],
        },
        ensure_ascii=False,
    )
    doc_bad = parse_script(bad, source="bad.json")
    warn = " ".join(doc_bad.warnings)
    check("自检：start_scene 不存在被发现", "start_scene" in warn, warn)
    check("自检：出口断链被发现", "ghost" in warn, warn)
    check("自检：不可达场景被发现", "b" in warn, warn)
    check("自检：缺 system 段被发现", "system" in warn, warn)
    check("自检后 start_scene 已回退到可用场景", doc_bad.start_scene == "a", doc_bad.start_scene)

    # --- 11. 场景字段缺省补齐（用户手写剧本也能跑）---
    doc_min = parse_script(json.dumps({"title": "极简", "scenes": [{"id": "only"}]}, ensure_ascii=False))
    sc = doc_min.data["scenes"][0]
    check("缺省字段被补齐（title/location/public_text/exits）",
          all(k in sc for k in ("title", "location", "public_text", "exits", "checks")), str(sc))

    # --- 12. 小助手拿到的次级 prompt 确实是公开版 ---
    check("两个 Agent 的次级 prompt 不同（DM 完整 / 小助手公开）",
          n_agent.secondary_prompt(NarratorContext(state=state)) != a_agent.secondary_prompt(AdvisorContext(state=state)),
          "")
    check("小助手的次级 prompt = 公开版",
          a_agent.secondary_prompt(AdvisorContext(state=state)) == st.script_prompt_public, "")

    # --- 13. 相对路径标签不会把本机绝对路径写进提示词 ---
    p_no_label = build_script_prompt(ScriptStore(TOTSK).doc, "dm")
    check("未传 label 时退回原路径（仅本地场景）", "totsk_l1.json" in p_no_label, "")


class FakeProvider:
    """假 Provider：按脚本依次吐出预设回复，用来在没有真实模型时验证 Agent 工具循环。
    `replies` 里每一项要么是 {"content","tool_calls"}，要么是一个可调用对象
    （拿 messages/tools 现场算回复），后者用于动态断言。
    """

    name = "fake"
    supports_tools = True

    def __init__(self, replies, allow_tools=True):
        self.replies = list(replies)
        self.seen = []  # [(kind, messages, tools?)]
        self.allow_tools = allow_tools

    async def chat(self, messages, json_mode=False):
        self.seen.append(("chat", messages))
        r = self.replies.pop(0) if self.replies else ""
        if callable(r):
            return await r(messages, json_mode)
        return r if isinstance(r, str) else (r.get("content") or "")

    async def chat_with_tools(self, messages, tools):
        self.seen.append(("tools", messages, tools))
        if not self.allow_tools:
            raise ToolCallingUnsupported("fake: 该模型不支持工具调用")
        r = self.replies.pop(0) if self.replies else {}
        return r

    async def close(self):
        pass


def tool_call(name, args=None, cid=None):
    return {
        "id": cid or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)},
    }


def _reply(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


async def run_unit_agents():
    """Agent 层校验：工具循环、护栏、降级路径、槽位清洗 —— 全部不需要真实模型。"""
    print("== 进程内校验：Agent 工具循环与降级（无真实模型） ==")
    store = ScriptStore(SCRIPT)
    start = store.start_scene
    first_exit = store.scenes[start]["exits"][0]["to"]
    first_check = (store.scenes[start].get("checks") or [{}])[0].get("id")

    # --- 1. 标准工具循环：模型调 move_to → 回灌结果 → 再调 finish ---
    state = GameState(store, start)
    state.add_player("亚瑟", {"cls": "战士"})
    fake = FakeProvider(
        [
            _reply(tool_call("move_to", {"scene_id": first_exit})),
            _reply(tool_call("finish", {"narration": "你推门而入。"})),
        ]
    )
    res = await NarratorAgent(store, fake).run(
        NarratorContext(state=state, player_name="亚瑟"), "推门进去"
    )
    check("Agent：工具循环真的改了状态（move_to 生效）", state.current_scene == first_exit, state.current_scene)
    check("Agent：finish 收尾给出旁白", res.text == "你推门而入。", res.text)
    check("Agent：trace 按序记录工具", res.used_tools == ["move_to", "finish"], str(res.used_tools))
    check("Agent：source 标为 llm", res.source == "llm", res.source)
    second_msgs = fake.seen[1][1]
    check(
        "Agent：工具结果以 role=tool 回灌给模型",
        any(m.get("role") == "tool" for m in second_msgs),
        str([m.get("role") for m in second_msgs]),
    )
    check(
        "Agent：工具声明下发给模型（含 move_to）",
        bool(fake.seen[0][2]) and any(t["function"]["name"] == "move_to" for t in fake.seen[0][2]),
        "",
    )

    # --- 2. 护栏：非法出口被工具拒绝，状态不变 ---
    s2 = GameState(store, start)
    s2.add_player("A", {"cls": "战士"})
    fake2 = FakeProvider([_reply(tool_call("move_to", {"scene_id": "atlantis"})), _reply(tool_call("finish", {"narration": "没有这条路。"}))])
    res2 = await NarratorAgent(store, fake2).run(NarratorContext(state=s2, player_name="A"), "去亚特兰蒂斯")
    check("Agent 护栏：不存在的出口被拒、场景不变", s2.current_scene == start, s2.current_scene)
    check(
        "Agent 护栏：拒绝以 error 回灌（模型可重试）",
        "error" in json.dumps(res2.steps[0].result, ensure_ascii=False),
        str(res2.steps[0].result)[:80],
    )

    # --- 3. 护栏：未声明的 flag 被拒 ---
    s3 = GameState(store, start)
    s3.add_player("A", {"cls": "战士"})
    fake3 = FakeProvider([_reply(tool_call("set_flag", {"flag": "made_up_flag"})), _reply(tool_call("finish", {"narration": "嗯。"}))])
    await NarratorAgent(store, fake3).run(NarratorContext(state=s3, player_name="A"), "乱记一笔")
    check("Agent 护栏：剧本没声明的 flag 记不进去", "made_up_flag" not in s3.flags, str(sorted(s3.flags)))

    # --- 4. 骰子只能由服务端投（roll_check 产生 dice 事件） ---
    if first_check:
        s4 = GameState(store, start)
        s4.add_player("A", {"cls": "战士"})
        dice_seed(20261009)
        fake4 = FakeProvider([_reply(tool_call("roll_check", {"check_id": first_check})), _reply(tool_call("finish", {"narration": "你仔细查看。"}))])
        ctx4 = NarratorContext(state=s4, player_name="A")
        await NarratorAgent(store, fake4).run(ctx4, "我说服老周")
        dice_evs = [e for e in ctx4.events if e.get("type") == "dice"]
        check("Agent：roll_check 由服务端投骰并产出 dice 事件", len(dice_evs) == 1, str(ctx4.events)[:100])
        check("Agent：投骰结果不由模型编造（含真实 roll 值）", bool(dice_evs) and isinstance(dice_evs[0].get("roll"), int), str(dice_evs)[:80])

    # --- 5. 权限边界：小助手的工具集里没有写工具 ---
    adv = AdvisorAgent(store, None)
    adv_tools = adv.build_tools(AdvisorContext(state=GameState(store, start)))
    check("小助手：工具全部只读", all(t.read_only for t in adv_tools), str([(t.name, t.read_only) for t in adv_tools]))
    check(
        "小助手：结构上拿不到写工具",
        not any(t.name in ("move_to", "set_flag", "roll_check") for t in adv_tools),
        str([t.name for t in adv_tools]),
    )
    check(
        "小助手：不再有 read_scene / list_valid_actions（信息已在 user 消息里）",
        {t.name for t in adv_tools} == {"lookup_script", "finish"},
        str([t.name for t in adv_tools]),
    )
    adv_ctx5 = AdvisorContext(state=GameState(store, start))
    adv_user = adv.build_user_message(adv_ctx5)
    check("小助手：user 消息带「可走的方向」与「可尝试的检定」",
          "可走的方向：" in adv_user and "可尝试的检定：" in adv_user, adv_user[:160])
    check("小助手：user 消息不含 DM 内幕（dm_notes）", "dm_notes" not in adv_user, "")

    # --- 5b. 小助手：user 消息带「最近发生」（有档案时），合法选项带触发说法 ---
    class _FakeCh:
        def digest(self, n):
            return "【最近发生】李维 尝试：我说服老周"

    adv_user_h = adv.build_user_message(
        AdvisorContext(state=GameState(store, start), chronicle=_FakeCh())
    )
    check("小助手：有档案时 user 消息带「最近发生」",
          "最近发生：" in adv_user_h and "说服老周" in adv_user_h, adv_user_h[:140])
    check("小助手：无档案时不出现剧情块", "最近发生：" not in adv_user, "")
    check("小助手：合法选项带触发说法（建议措辞可贴近它）",
          "触发说法" in adv_user_h, adv_user_h[adv_user_h.find("可走的方向："):][:120])
    check("小助手：user 消息不带出口 label 原文（防照抄）",
          "钻进石门，走进甬道" not in adv_user_h, adv_user_h[adv_user_h.find("可走的方向："):][:120])

    # 校验层确定性过滤：照抄 label / 不足 8 字的建议被丢弃
    fake_ctx = AdvisorContext(state=GameState(store, start))
    raw = {"options": ["钻进石门", "询问卡特", "顺着门楣浮雕查看被敲掉的毒牙"]}
    from server.agents import advisor as advisors_mod
    v = advisors_mod._advise_validate(adv, raw, fake_ctx)
    check("小助手：校验层丢弃照抄清单原文的短语",
          v["options"] == ["顺着门楣浮雕查看被敲掉的毒牙"], str(v))
    raw2 = {"options": ["钻进石门下墓，顺着拖痕找", "用风灯照亮夜空"]}
    v2 = advisors_mod._advise_validate(adv, raw2, fake_ctx)
    check("小助手：校验层只留含触发词的建议（机制可达）",
          v2["options"] == ["钻进石门下墓，顺着拖痕找"], str(v2))

    # --- 6. 小助手：接了模型走工具循环，没接就显式失败（取消脚本化兜底） ---
    s6 = GameState(store, start)
    s6.add_player("A", {"cls": "法师"})
    adv_fake = FakeProvider([_reply(tool_call("finish", {"options": ["检查石门", "观察四周", "听一听"]}))])
    r6 = await AdvisorAgent(store, adv_fake).suggest(AdvisorContext(state=s6, player_name="A"))
    check("小助手：模型给的建议入列", r6.data.get("options") == ["检查石门", "观察四周", "听一听"], str(r6.data))
    check("小助手：用了 finish 工具", r6.used_tools == ["finish"], str(r6.used_tools))
    r6b = await AdvisorAgent(store, None).suggest(AdvisorContext(state=s6, player_name="A"))
    check(
        "小助手：无模型时不给建议（显式失败）",
        not r6b.data.get("options") and r6b.source == "failed",
        f"{r6b.data} / {r6b.source}",
    )

    # --- 7. 无 provider：DM 显式失败（取消脚本化兜底，2026-10-13 拍板）---
    s7 = GameState(store, start)
    s7.add_player("A", {"cls": "战士"})
    n7 = NarratorAgent(store, None)
    ctx7 = NarratorContext(state=s7, player_name="A")
    r7 = await n7.run(ctx7, "我环顾四周，警惕地观察")
    check("无模型：stopped=no_provider", r7.stopped == "no_provider", r7.stopped)
    check("无模型：source=failed", r7.source == "failed", r7.source)
    check("无模型：不产出任何旁白", not (r7.text or "").strip(), repr(r7.text))
    check("无模型：不产出可广播的事件", not ctx7.events, str(ctx7.events)[:100])
    check("无模型：状态没有被改动", s7.current_scene == start, s7.current_scene)
    check("无模型：不经过 provider.chat", not isinstance(n7.provider, FakeProvider) and n7.provider is None, str(n7.provider))

    # --- 8. 模型不支持工具调用 → 改用单轮 JSON 协议（仍是真的跑了模型，source=llm）---
    s8 = GameState(store, start)
    s8.add_player("A", {"cls": "战士"})
    no_tools = FakeProvider(
        [_reply(tool_call("move_to", {"scene_id": first_exit}))],
        allow_tools=False,
    )

    async def _json_reply(messages, json_mode=False):
        return json.dumps({"narration": "你环顾四周。", "move_to": first_exit, "set_flags": []}, ensure_ascii=False)

    no_tools.replies = [_json_reply]
    r8 = await NarratorAgent(store, no_tools).run(NarratorContext(state=s8, player_name="A"), "进去看看")
    check("单轮 JSON：模型不支持工具 → 协议生效", s8.current_scene == first_exit, s8.current_scene)
    check("单轮 JSON：旁白取自 JSON", "环顾四周" in r8.text, r8.text)
    check("单轮 JSON：stopped=no_tools / source=llm", r8.stopped == "no_tools" and r8.source == "llm", f"{r8.stopped}/{r8.source}")

    # --- 9. Provider 槽位矩阵 ---
    check("provider：kind=off → None", build_provider({"kind": "off"}) is None, "")
    check("provider：云 API 缺 key → None", build_provider({"kind": "cloud", "base_url": "https://x/v1", "model": "m"}) is None, "")
    p_cloud = build_provider({"kind": "cloud", "base_url": "https://x/v1", "model": "m", "api_key": "sk-1"})
    check("provider：云 API 齐活 → 可用", p_cloud is not None and p_cloud.label == "cloud", "")
    if p_cloud:
        await p_cloud.close()
    p_local = build_provider({"kind": "local", "base_url": "http://127.0.0.1:8080/v1", "model": "m"})
    check("provider：本地模型免 key → 可用", p_local is not None and p_local.label == "local", "")
    if p_local:
        await p_local.close()

    # --- 9b. llamacpp 通道（全部离线：不拉子进程，构建是懒的） ---
    from server import llamacpp as llm_cpp
    from server.engine import find_model

    exe = llm_cpp.find_llama_server()
    check("llamacpp：自动探测到 llama-server（tools/src/）", exe is not None and exe.is_file(),
          str(exe))
    args_gpu = llm_cpp.server_args("m.gguf", 12345, ngl=99, context=6144)
    check("llamacpp：启动参数含 -m/--port/-c/-ngl/--jinja",
          all(a in args_gpu for a in ("-m", "m.gguf", "12345", "-c", "6144", "-ngl", "99", "--jinja")),
          " ".join(args_gpu))
    args_cpu = llm_cpp.server_args("m.gguf", 1, ngl=0, context=1)
    check("llamacpp：-ngl 落在参数里且值随 backend 走",
          args_cpu[args_cpu.index("-ngl") + 1] == "0" and args_gpu[args_gpu.index("-ngl") + 1] == "99",
          " ".join(args_cpu))
    check("llamacpp：server 路径写错 → build_provider None",
          build_provider({"kind": "llamacpp", "server": "Z:/nope/llama-server.exe"}) is None, "")
    weights = find_model("")   # 本机没权重时跳过构建断言（CI 友好）
    if exe is not None and weights is not None:
        p_lp = build_provider({"kind": "llamacpp", "server": str(exe),
                               "model": str(weights), "backend": "gpu", "context": 6144})
        check("llamacpp：构建成功且是懒的（不拉进程）",
              p_lp is not None and p_lp._handle is None and p_lp.supports_tools, str(p_lp and p_lp.name))
        check("llamacpp：describe 不含绝对路径",
              "/" not in (p_lp.describe().get("model") or "") and "\\" not in (p_lp.describe().get("model") or ""),
              str(p_lp.describe()))
        await p_lp.close()   # 没拉过进程 → release 是安全的空操作
        check("llamacpp：close 未拉进程时安全", True, "")
    p_alias = build_provider({"kind": "llama", "server": "Z:/nope/llama-server.exe"})
    check("llamacpp：kind 别名 llama 也认（但 server 不在 → None）", p_alias is None, "")

    # --- 10. 槽位清洗：白名单 + 类型 + 取值范围 ---
    from server.main import clean_slot_patch, merge_slot

    dirty = clean_slot_patch(
        {
            "kind": "HACKED",
            "base_url": "ftp://evil",
            "model": "  m1  ",
            "temperature": 99,
            "api_key": " sk-x ",
            "drop_table": True,
        }
    )
    check("清洗：非法 kind 被丢", "kind" not in dirty, str(dirty))
    check("清洗：非 http(s) 的 base_url 被丢", "base_url" not in dirty, str(dirty))
    check("清洗：白名单外的字段被丢", "drop_table" not in dirty, str(dirty))
    check("清洗：temperature 被夹到 [0,2]", dirty.get("temperature") == 2.0, str(dirty.get("temperature")))
    check("清洗：字符串两端空白被去掉", dirty.get("model") == "m1" and dirty.get("api_key") == "sk-x", str(dirty))
    slot = {"kind": "cloud", "api_key": "sk-old", "model": "a"}
    merge_slot(slot, {"model": "b"})
    check("合并：未提交的 api_key 保持原值", slot["api_key"] == "sk-old", str(slot))

    # --- 11. 对外状态描述绝不泄露 api_key ---
    pub = describe_agents(
        {"kind": "cloud", "base_url": "https://x/v1", "model": "m", "api_key": "sk-secret"},
        {"kind": "off", "api_key": ""},
    )
    blob = json.dumps(pub, ensure_ascii=False)
    check(
        "对外状态：不暴露 api_key 字段（只留 has_api_key 布尔）",
        all("api_key" not in (pub[k] or {}) for k in ("dm", "advisor")),
        blob[:110],
    )
    check("对外状态：不含明文密钥内容", "sk-secret" not in blob, blob[:110])
    check("对外状态：只暴露 has_api_key 布尔", pub["dm"].get("has_api_key") is True, str(pub["dm"]))
    check("对外状态：云 API 齐活即 ready", pub["dm"].get("ready") is True and pub["dm"].get("mode") == "model", str(pub["dm"]))
    check("对外状态：留空即 off", pub["advisor"].get("ready") is False and pub["advisor"].get("mode") == "off", str(pub["advisor"]))

    # --- 12. 通用任务引擎：单轮 JSON 任务（无工具）走「校验 → 失败」 ---
    class _DummyTaskAgent(ToolAgent):
        def __init__(self, provider=None):
            super().__init__(store=None, provider=provider)
            self.default_task = "dummy"
            self.register(
                Task(
                    name="dummy",
                    system=lambda a, c: "你是测试任务。",
                    user=lambda a, c: "给点东西",
                    json_hint='只输出 JSON：{"items":["..."]}',
                    validate=lambda a, d, c: {"items": [str(x) for x in (d.get("items") or [])][:3]},
                )
            )

    r_none = await _DummyTaskAgent(None).run_task("dummy", None)
    check(
        "引擎：无模型的任务 → 显式失败，不产兜底数据",
        not r_none.data and r_none.source == "failed",
        f"{r_none.data}/{r_none.source}",
    )
    fake_single = FakeProvider([json.dumps({"items": ["甲", "乙"], "extra": 1}, ensure_ascii=False)])
    r_ok = await _DummyTaskAgent(fake_single).run_task("dummy", None)
    check("引擎：单轮 JSON 任务取回结构化数据", r_ok.data.get("items") == ["甲", "乙"] and r_ok.source == "llm", str(r_ok.data))
    check("引擎：校验器裁剪生效（白名单外的字段被丢）", "extra" not in r_ok.data, str(r_ok.data))
    check(
        "引擎：无工具任务只发一次 chat（不进工具循环）",
        len([s for s in fake_single.seen if s[0] == "chat"]) == 1 and not any(s[0] == "tools" for s in fake_single.seen),
        str([s[0] for s in fake_single.seen]),
    )
    r_bad = await _DummyTaskAgent(FakeProvider(["这不是 JSON"])).run_task("dummy", None)
    check(
        "引擎：模型输出不合规 → 显式失败（不再回落兜底）",
        not r_bad.data and r_bad.source == "failed",
        f"{r_bad.data}/{r_bad.source}",
    )
    check(
        "引擎：未知任务名 → 明确报错而非静默",
        (await _DummyTaskAgent(None).run_task("no_such", None)).stopped == "error",
        "",
    )


async def run_unit_npc():
    """NPC 子系统校验：人物卡、别名、反应采样、记忆隔离、DM 只见卡不见流水账。"""
    print("== 进程内校验：NPC 子系统（人物卡 / 反应权重 / 记忆隔离） ==")
    store = ScriptStore(SCRIPT)
    start = store.start_scene

    # --- 1. 剧本原生 NPC 被播种（它们本来就有身份/风格，不消耗模型调用）---
    svc = NpcService(store, agent=None)
    zhou = svc.registry.resolve("老周")
    check("NPC：剧本原生 NPC 被播种", zhou is not None and zhou.role == "古堡看守", str(svc.index()))
    check("NPC：原生卡带原型与特质", bool(zhou.traits) and zhou.from_script, str(zhou.to_dict())[:80])

    # --- 2. 无模型：为剧本外 NPC 建档 → 原型兜底 ---
    check("NPC：原型推断（摊贩→平民）", infer_archetype("摊贩") in ("平民", "商人"), infer_archetype("摊贩"))
    card = await svc.ensure_card("王二", role="沿街摊贩")
    check("NPC：无模型也能建档（原型兜底）", card is not None and not card.generated, str(card.to_dict())[:80])
    check("NPC：兜底卡带完整特质轴", all(k in card.traits for k in ARCHETYPES[card.archetype]["traits"]), str(card.traits))

    # --- 3. 有模型：persona 任务生成卡片（特质 clamp、别名入库）---
    persona_json = json.dumps(
        {
            "name": "阿福",
            "role": "客栈跑堂",
            "summary": "机灵但胆小，见谁都陪笑。",
            "traits": {"胆量": 1.7, "攻击性": -0.5, "守序": 0.6, "记仇": 0.2, "贪财": 0.7, "好说话": 0.8},
            "voice": "点头哈腰，句尾常带「客官」。",
            "goals": ["攒钱盘下客栈"],
            "fears": ["挨打", "丢差事"],
            "knows": "后院住着一位不肯露面的客人",
            "aliases": ["小福子"],
        },
        ensure_ascii=False,
    )
    svc2 = NpcService(store, agent=AssistantAgent(store, FakeProvider([persona_json])))
    afu = await svc2.ensure_card("阿福", role="客栈跑堂")
    check("NPC：模型生成人物卡", afu is not None and afu.generated, str(afu.to_dict())[:80])
    check("NPC：超范围特质被夹到 [0,1]", afu.traits["胆量"] == 1.0 and afu.traits["攻击性"] == 0.0, str(afu.traits))
    check("NPC：模型给的别称入索引（外号可命中）", svc2.registry.resolve("小福子") is afu, str(afu.aliases))

    # --- 4. 别名：玩家给外号 → 归并 + 文本识别 ---
    check("NPC：新增外号可解析", svc2.registry.add_alias(afu.id, "小跑堂") and svc2.registry.resolve("小跑堂") is afu, "")
    hits = svc2.registry.detect("我拍了下小跑堂的肩膀")
    check("NPC：从行动文本里认出 NPC（含外号）", any(c.id == afu.id for c in hits), str([c.name for c in hits]))

    # --- 5. react：权重的来源与「骰点不出自模型」---
    dice.seed(20261010)
    r1 = await svc2.react(zhou, "亚瑟 对 老周：「我一拳打在老周脸上」")
    check("NPC：反应采样给出候选与落点", bool(r1["options"]) and r1["chosen"] in [o["label"] for o in r1["options"]], str(r1))
    dice.seed(20261010)
    r2 = await svc2.react(zhou, "亚瑟 对 老周：「我一拳打在老周脸上」")
    check("NPC：同一骰子种子可复现同一次采样", r1["chosen"] == r2["chosen"] and r1["roll"] == r2["roll"], f"{r1}/{r2}")
    # 模型只给权重、不出骰点：权重被原样采纳，落点由 dice 决定
    react_json = json.dumps({"options": [{"label": "忍气吞声", "weight": 0.9}, {"label": "呼救报官", "weight": 0.1}]}, ensure_ascii=False)
    # 第一个回复留给 ensure_card 的 persona（给空对象 → 原型兜底），第二个给 react
    svc3 = NpcService(store, agent=AssistantAgent(store, FakeProvider(["{}", react_json])))
    c3 = await svc3.ensure_card("李四", role="平民")
    dice.seed(7)
    r3 = await svc3.react(c3, "有人推了李四一把")
    check("NPC：模型给出的权重被采纳", [o["label"] for o in r3["options"]] == ["忍气吞声", "呼救报官"], str(r3["options"]))
    dice.seed(7)
    check("NPC：落点由服务端骰子决定（可复现）", dice.weighted_pick([0.9, 0.1])[0] == dice.weighted_pick([0.9, 0.1])[0], "")
    check("NPC：模型输出里没有骰点字段", "roll" not in react_json and "dice" not in react_json, "")

    # --- 6. 骰子采样边界 ---
    check("骰子：空权重表 → (-1,0,0)", dice.weighted_pick([]) == (-1, 0.0, 0.0), str(dice.weighted_pick([])))
    dice.seed(3)
    idx0 = dice.weighted_pick([0, 0, 0])[0]
    check("骰子：权重全 0 → 退化为等概率（落在合法范围）", 0 <= idx0 <= 2, str(idx0))

    # --- 7. 记忆隔离：DM 只见「人物卡 + 快照」，不见流水账 ---
    svc.remember(zhou.id, "亚瑟", "亚瑟一拳打在老周脸上（秘密流水账）", turn=3, polarity=-0.25)
    pub = svc.card_public(zhou)
    check("NPC：对外快照带好感度（提炼结论）", "status" in pub, str(pub.get("status")))
    brief = svc.briefs_for([zhou])
    check("NPC：给 DM 的素材不含记忆流水账", "秘密流水账" not in brief, brief[:80])
    check("NPC：给 DM 的素材含人物卡要点", zhou.name in brief and (zhou.summary[:6] in brief), brief[:80])
    # 放进 DM 的实际请求里也不该出现流水账
    state = GameState(store, start)
    state.add_player("亚瑟", {"cls": "战士"})
    ctx = NarratorContext(state=state, player_name="亚瑟", npc_brief=brief)
    fake = FakeProvider([_reply(tool_call("finish", {"narration": "老周缩了缩脖子。"}))])
    await NarratorAgent(store, fake, npc=svc).run(ctx, "我打了老周")
    sent_blob = json.dumps(fake.seen[0][1], ensure_ascii=False)
    check("NPC：DM 请求里含人物卡", "老周" in sent_blob, "")
    check("NPC：DM 请求里不含记忆流水账", "秘密流水账" not in sent_blob, "")

    # --- 8. 工具：接了 NPC 子系统才有 npc_card / npc_introduce ---
    n_with = NarratorAgent(store, None, npc=svc)
    names_with = [t.name for t in n_with.build_tools(ctx)]
    n_without = NarratorAgent(store, None)
    names_without = [t.name for t in n_without.build_tools(ctx)]
    check("NPC：接子系统的 DM 有 npc_card / npc_introduce 工具",
          "npc_card" in names_with and "npc_introduce" in names_with, str(names_with))
    check("NPC：没接子系统的 DM 没有 NPC 工具",
          "npc_card" not in names_without and "npc_introduce" not in names_without, str(names_without))
    check("NPC：npc_card 不下发记忆流水账",
          "秘密流水账" not in json.dumps(n_with._t_npc_card(ctx, "老周"), ensure_ascii=False), "")

    # --- 9. 反应选项数封顶 + 空选项走兜底 ---
    many = json.dumps({"options": [{"label": f"选项{i}", "weight": 0.1} for i in range(9)]}, ensure_ascii=False)
    svc4 = NpcService(store, agent=AssistantAgent(store, FakeProvider(["{}", many])))
    c4 = await svc4.ensure_card("赵五", role="卫兵")
    r4 = await svc4.react(c4, "有人骂了赵五")
    check("NPC：反应选项数被截到 5 以内", len(r4["options"]) <= 5, str(len(r4["options"])))
    svc5 = NpcService(store, agent=AssistantAgent(store, FakeProvider(["{}", json.dumps({"options": []})])))
    c5 = await svc5.ensure_card("钱六", role="商人")
    r5 = await svc5.react(c5, "有人抢了钱六的钱袋")
    check("NPC：模型给空选项 → 回落原型 stance", bool(r5["options"]) and r5["source"] == "fallback", str(r5)[:100])


async def run_unit_chronicle():
    """剧情档案：只增落盘、幂等、BM25/向量混合检索、滚动摘要、DM 只读工具。"""
    print("== 进程内校验：剧情档案与混合检索（P3） ==")
    store = ScriptStore(SCRIPT)
    tmp = Path(__file__).resolve().parent / "_chronicle_test"
    tmp.mkdir(exist_ok=True)
    jsonl = tmp / "c.jsonl"

    # --- 1. 只增落盘 + 幂等 ---
    if jsonl.exists():
        jsonl.unlink()
    (tmp / "c.meta.json").unlink(missing_ok=True)
    ch = Chronicle(jsonl)
    ch.add("老周是古堡的看守，成天打瞌睡。", turn=1, kind="narration", scene="gate", actors=["老周"])
    again = ch.add("老周是古堡的看守，成天打瞌睡。", turn=1, kind="narration", scene="gate", actors=["老周"])
    ch.add("亚瑟 尝试：推开铁门", turn=2, kind="action", scene="gate", actors=["亚瑟"])
    ch.add("亚瑟 的 力量 检定 DC12 掷出 15，成功", turn=2, kind="dice", actors=["亚瑟"])
    check("档案：同内容重复写入被幂等跳过", again is None and len(ch) == 3, str(len(ch)))
    check("档案：JSONL 真的落盘", jsonl.exists() and len(jsonl.read_text(encoding="utf-8").strip().splitlines()) == 3,
          str(jsonl.stat().st_size) if jsonl.exists() else "无文件")

    # --- 2. 载入重建 ---
    ch2 = Chronicle(jsonl)
    check("档案：可从 JSONL 重建", len(ch2) == 3 and ch2.entries[0]["actors"] == ["老周"], str(len(ch2)))
    check("档案：条目字段完整", set(ch2.entries[0]) >= {"id", "turn", "kind", "scene", "actors", "text"},
          str(sorted(ch2.entries[0])))

    # --- 3. BM25 字面召回 ---
    hits = ch2.search("老周", k=3)
    check("档案：BM25 能按名字召回", bool(hits) and "老周" in hits[0]["text"], str(hits[:1])[:120])
    check("档案：kind=action 的玩家行动也被记录", any(e["kind"] == "action" for e in ch2.entries), "")

    # --- 4. 向量语义召回（有模型时才验；没下模型就退化成纯 BM25，不算失败） ---
    emb = Embedder()
    check("嵌入：bge-small-zh 已就绪或有明确降级原因", emb.available or bool(emb.reason), emb.reason)
    if emb.available:
        ch3 = Chronicle(tmp / "v.jsonl", embedder=emb)
        if (tmp / "v.jsonl").exists():
            (tmp / "v.jsonl").unlink()
        ch3 = Chronicle(tmp / "v.jsonl", embedder=emb)
        ch3.add("老周是古堡的看守，成天打瞌睡。", turn=1, kind="narration", actors=["老周"])
        ch3.add("亚瑟买了一把新的长剑。", turn=2, kind="action", actors=["亚瑟"])
        v = emb.encode(["古堡大门前的守卫"])
        check("嵌入：向量维度为 512", len(v[0]) == 512, str(len(v[0]) if v else 0))
        check("嵌入：已做 L2 归一化", abs(sum(x * x for x in v[0]) - 1.0) < 1e-3, "")
        # 用「守卫」问「看守」（近义），字面不一致，靠向量召回
        semantic = ch3.search("那个看大门的守卫是谁", k=1)
        check("档案：语义检索能召回近义表述", bool(semantic) and "老周" in semantic[0]["text"], str(semantic[:1])[:120])

    # --- 5. actor 过滤 ---
    only_a = ch2.search("铁门", k=3, actor="亚瑟")
    check("档案：actor 过滤只返回与该角色相关的记录", all("亚瑟" in (h["actors"] or []) or "亚瑟" in h["text"] for h in only_a),
          str(only_a)[:120])

    # --- 6. 热层 digest 与滚动摘要 ---
    ch2.set_summary("队伍已进入古堡大门，正在找暗门。", upto_turn=3)
    d = ch2.digest(4)
    check("档案：digest 含滚动摘要", "此前剧情概要" in d and "暗门" in d, d[:80])
    check("档案：digest 含最近发生", "最近发生" in d and "老周" in d, d[:120])
    ch4 = Chronicle(jsonl)
    check("档案：摘要随元数据持久化", ch4.summary.startswith("队伍已进入") and ch4.summary_upto == 3, ch4.summary)

    # --- 7. summarize 任务：有模型用模型输出，无模型显式失败（取消脚本化兜底）---
    a_np = AssistantAgent(store, None)
    check("摘要任务：已注册", a_np.has_task("summarize"), "")
    res_np = await a_np.run_task("summarize", SummaryCtx(previous="旧概要。", items=["甲打了乙", "乙跑掉了"]))
    check(
        "摘要任务：无模型不产摘要、标 failed",
        not (res_np.data or {}).get("summary") and res_np.source == "failed",
        f"{str(res_np.data)[:80]}/{res_np.source}",
    )

    good = json.dumps({"summary": "队伍打了乙一顿，乙逃走了。", "key_characters": ["乙"], "open_threads": ["乙的去向"]},
                      ensure_ascii=False)
    a_m = AssistantAgent(store, FakeProvider([good]))
    res_m = await a_m.run_task("summarize", SummaryCtx(previous="", items=["甲打了乙"]))
    check("摘要任务：模型输出被采纳", (res_m.data or {}).get("summary", "").startswith("队伍打了乙"), str(res_m.data)[:120])
    bad = AssistantAgent(store, FakeProvider(["{}"]))
    res_bad = await bad.run_task("summarize", SummaryCtx(previous="旧", items=["新"]))
    check("摘要任务：空摘要被校验拦下 → 显式失败（不回落兜底）",
          res_bad.source == "failed" and not (res_bad.data or {}).get("summary"),
          f"{res_bad.source}/{str(res_bad.data)[:60]}")
    longsum = json.dumps({"summary": "啊" * 2000}, ensure_ascii=False)
    a_long = AssistantAgent(store, FakeProvider([longsum]))
    res_long = await a_long.run_task("summarize", SummaryCtx(items=["x"]))
    check("摘要任务：超长摘要被截断", len(res_long.data.get("summary", "")) <= 1200, str(len(res_long.data.get("summary", ""))))

    # --- 8. DM 的 recall_history：有档案才有工具，且只读 ---
    state = GameState(store, store.start_scene)
    state.add_player("亚瑟", {"cls": "战士"})
    n_with = NarratorAgent(store, None, chronicle=ch2)
    tools_with = {t.name: t for t in n_with.build_tools(NarratorContext(state=state))}
    check("DM：有档案时挂上 recall_history", "recall_history" in tools_with, str(sorted(tools_with)))
    check("DM：recall_history 是只读工具", tools_with["recall_history"].read_only is True, "")
    hits2 = n_with._t_recall_history(NarratorContext(state=state), query="老周", k=3)
    check("DM：recall_history 返回档案摘录", bool(hits2.get("hits")) and "老周" in hits2["hits"][0]["text"], str(hits2)[:120])
    n_without = NarratorAgent(store, None)
    check("DM：无档案时不挂 recall_history", "recall_history" not in {t.name for t in n_without.build_tools(NarratorContext(state=state))}, "")

    # --- 9. 热层注入 user 消息（只给结论，不灌全文） ---
    msg = n_with.build_user_message(NarratorContext(state=state), "看看周围")
    check("DM：user 消息带内部记忆热层", "内部记忆：只用于理解" in msg, "")

    # --- 10. 预热：把「首次前向」的成本挪到等待室 ---
    #  实测 4B：2000 token 的首次生成 26 秒，同长度第二次 7-10 秒。预热就是空转一次，
    #  所以它必须**只发请求、不执行工具**（拿到 tool_calls 也丢掉）。
    wp = FakeProvider([_reply(tool_call("move_to", {"scene_id": "nowhere"}))])
    wp.needs_warmup = True
    st_w = GameState(store, store.start_scene)
    ok = await NarratorAgent(store, wp).warmup(NarratorContext(state=st_w, player_name="（预热）"))
    check("预热：真的发了一次请求", ok and len(wp.seen) == 1, str(len(wp.seen)))
    check("预热：请求里带工具声明（为了让 prefill 长度与真实一轮一致）",
          len(wp.seen[0]) == 3 and len(wp.seen[0][2]) > 0,
          str(len(wp.seen[0][2]) if len(wp.seen[0]) > 2 else 0))
    check("预热：模型回了 move_to，但场景没动（没有 handler 跑）",
          st_w.current_scene == store.start_scene, st_w.current_scene)
    check("预热：没有模型时直接跳过",
          await NarratorAgent(store, None).warmup(NarratorContext(state=st_w)) is False, "")
    check("预热：云 API 这类没有 needs_warmup 的 Provider 不参与",
          await NarratorAgent(store, FakeProvider([])).warmup(NarratorContext(state=st_w)) is False, "")

    # --- 11. 旁白护栏：旁白与权威结果打架时丢掉旁白 ---
    #  实跑 25 轮里，15 次检定有 4 次被写成成功；6 次推进里 6 次把已播报的原文又抄一遍。
    #  这两种都不该让玩家看到 —— 权威文案系统已经广播过了。
    from server import agents as _agents_pkg  # noqa: F401  (确保模块已加载)
    from server.agents import narrator as narrator_mod

    # 这两条要用「蛇王墓」这份剧本：它才有石闩与卡特。
    l1 = ScriptStore(str(Path(__file__).resolve().parent.parent / "scripts" / "totsk_l1.json"))
    l1_start = l1.start_scene
    check("护栏：『石闩被缓缓抬起』被判成成功语气",
          narrator_mod.asserts_success("石闩被缓缓抬起，铁钉随之上升。") == "抬起", "")
    check("护栏：『你未能发现』不算成功语气",
          narrator_mod.asserts_success("你未能发现任何机关的细节。") == "", "")
    check("护栏：『一无所获』不算成功语气",
          narrator_mod.asserts_success("你们扒了半天，一无所获。") == "", "")
    _pt = l1.get_scene(l1_start)["public_text"]
    check("护栏：整段复述场景原文能被识别",
          narrator_mod.echoes_text("他抬头看了看。" + _pt[:60], _pt) is True, "")
    check("护栏：只补一句衔接不算复述",
          narrator_mod.echoes_text("你推门走了进去，湿气扑面。", _pt) is False, "")

    _orig_roll = narrator_mod.dice.roll_check
    try:
        # 11a. 检定失败 + 旁白写成功
        narrator_mod.dice.roll_check = lambda skill, dc, mod=0: {
            "skill": skill, "dc": dc, "roll": 1, "mod": mod, "total": 1 + mod, "success": False,
        }
        st_f = GameState(l1, l1_start)
        st_f.add_player("测试员", {})
        fp = FakeProvider([
            _reply(tool_call("roll_check", {"check_id": "ask_patron"})),
            _reply(tool_call("finish", {"narration": "卡特点了点头——你发现了铭文的秘密。"})),
        ])
        ctx_f = NarratorContext(state=st_f, player_name="测试员")
        res_f = await NarratorAgent(l1, fp).run(ctx_f, "我向卡特打听铭文的来历")
        check("护栏：检定失败却写成成功 → 丢掉模型那段",
              res_f.text == "" and "失败" in getattr(res_f, "drop_reason", ""),
              f"text={res_f.text!r} reason={getattr(res_f, 'drop_reason', '')}")
        check("护栏：丢掉旁白不影响权威骰子事件",
              any(e.get("type") == "dice" and e.get("success") is False for e in ctx_f.events), "")
        check("护栏：权威失败文案仍在（玩家不会少信息）",
              any("卡特" in p for p in ctx_f.narration_parts), str(ctx_f.narration_parts)[:60])

        # 11b. 换场景 + 旁白把新场景原文又抄一遍
        fp2 = FakeProvider([
            _reply(tool_call("move_to", {"scene_id": "entrance_hall"})),
            _reply(tool_call("finish", {"narration": l1.get_scene("entrance_hall")["public_text"]})),
        ])
        st_e = GameState(l1, l1_start)
        st_e.add_player("测试员", {})
        res_e = await NarratorAgent(l1, fp2).run(
            NarratorContext(state=st_e, player_name="测试员"), "我钻进石门")
        check("护栏：推进后复述新场景原文 → 丢掉模型那段",
              res_e.text == "" and "复述" in getattr(res_e, "drop_reason", ""),
              f"text={res_e.text[:40]!r} reason={getattr(res_e, 'drop_reason', '')}")
        check("护栏：场景确实推进了", st_e.current_scene == "entrance_hall", st_e.current_scene)

        # 11c. 模型把热层里的旧旁白抄回来（越界局实测：连续两轮一字不差）
        ch_g = Chronicle(tmp / "g.jsonl")
        ch_g.add("李维的火堆在雨中摇曳，却无法驱散阴冷。", turn=1, kind="narration")
        fp3 = FakeProvider([_reply(tool_call("finish", {"narration": "李维的火堆在雨中摇曳，却无法驱散阴冷。"}))])
        st_g = GameState(l1, l1_start)
        st_g.add_player("测试员", {})
        res_g = await NarratorAgent(l1, fp3, chronicle=ch_g).run(
            NarratorContext(state=st_g, player_name="测试员"), "我生火")
        check("护栏：抄旧旁白 → 丢掉并说明原因",
              res_g.text == "" and "重复" in getattr(res_g, "drop_reason", ""),
              getattr(res_g, "drop_reason", ""))
        res_i = await NarratorAgent(l1, FakeProvider([
            _reply(tool_call("finish", {"narration": "系统已广播新场景原文。甬道深处传来低鸣。"}))]
        )).run(NarratorContext(state=st_g, player_name="测试员"), "我往前走")
        check("护栏：把系统指令念成旁白 → 丢掉",
              res_i.text == "" and "指令" in getattr(res_i, "drop_reason", ""),
              getattr(res_i, "drop_reason", ""))
        res_h = await NarratorAgent(l1, FakeProvider([
            _reply(tool_call("finish", {"narration": "李维蹲下拨开碎石，露出一道浅浅的划痕。"}))]
        )).run(NarratorContext(state=st_g, player_name="测试员"), "我拨开碎石")
        check("护栏：没有档案可抄时不会误判", res_h.text != "", res_h.text[:24])
    finally:
        narrator_mod.dice.roll_check = _orig_roll

    # --- 12. 检定关键词门：动作不命中触发说法 → 拒绝投骰并列出触发说法 ---
    st_k = GameState(l1, l1_start)
    st_k.add_player("测试员", {})
    fp_k = FakeProvider([
        _reply(tool_call("roll_check", {"check_id": "ask_patron"})),
        _reply(tool_call("finish", {"narration": "你在原地蹦了两下，没人理你。"})),
    ])
    ctx_k = NarratorContext(state=st_k, player_name="测试员")
    res_k = await NarratorAgent(l1, fp_k).run(ctx_k, "我在门口跳个舞")
    step_k = next((s for s in res_k.steps if s.tool == "roll_check"), None)
    check("检定门：动作不命中触发说法 → 拒绝投骰",
          step_k is not None and "不匹配" in str(step_k.result.get("error", "")),
          str(step_k.result if step_k else res_k.steps)[:120])
    check("检定门：拒绝时列出该检定的触发说法",
          step_k is not None and "打听" in step_k.result.get("triggered_by", []),
          str(step_k.result if step_k else "")[:120])
    check("检定门：被拒绝的检定不产生 dice 事件",
          not any(e.get("type") == "dice" for e in ctx_k.events), str(ctx_k.events)[:80])
    # 命中触发说法 → 正常投骰
    st_k2 = GameState(l1, l1_start)
    st_k2.add_player("测试员", {})
    dice_seed(20261021)
    fp_k2 = FakeProvider([
        _reply(tool_call("roll_check", {"check_id": "ask_patron", "trigger": "打听"})),
        _reply(tool_call("finish", {"narration": ""})),
    ])
    ctx_k2 = NarratorContext(state=st_k2, player_name="测试员")
    await NarratorAgent(l1, fp_k2).run(ctx_k2, "我向卡特打听这座墓的来历")
    check("检定门：命中触发说法 → 照常投骰",
          any(e.get("type") == "dice" for e in ctx_k2.events), str(ctx_k2.events)[:80])
    # 无 keywords 的检定不受门限制（向后兼容）
    gate_store = ScriptStore(SCRIPT)
    gate_checks = gate_store.scenes["gate"].get("checks", [])
    has_kw = any(c.get("keywords") for c in gate_checks)
    check("检定门：剧本声明 keywords 才生效（本剧本已声明）", has_kw, str(has_kw))

    # --- 13. 元问题入口分流：不进模型，直接回固定答案 ---
    st_m = GameState(l1, l1_start)
    st_m.add_player("测试员", {})
    fp_m = FakeProvider([_reply(tool_call("finish", {"narration": "不该被调用"}))])
    res_m = await NarratorAgent(l1, fp_m).run(NarratorContext(state=st_m, player_name="测试员"), "能存档吗")
    check("元问题：「能存档吗」入口直接回答，不调模型",
          res_m.source == "system" and res_m.stopped == "meta" and "服务器内存" in res_m.text,
          f"source={res_m.source} text={res_m.text[:40]!r}")
    check("元问题：分流不消耗一次模型调用", len(fp_m.seen) == 0, str(fp_m.seen)[:80])
    res_m2 = await NarratorAgent(l1, FakeProvider([_reply(tool_call("finish", {"narration": "x"}))])).run(
        NarratorContext(state=GameState(l1, l1_start), player_name="测试员"), "你是AI吗")
    check("元问题：「你是AI吗」也走分流",
          res_m2.source == "system" and "AI 扮演" in res_m2.text, res_m2.text[:60])
    # 行动型输入不被误伤（「我帮助卡特」含"帮助"但不是问规则）
    check("元问题：戏内行动不误伤",
          narrator_mod.meta_answer("我帮助卡特查看浮雕") == "", "")
    check("元问题：长句不误伤",
          narrator_mod.meta_answer("我仔细回忆此前的规则，看看能不能用上") == "", "")

    # --- 14. 不可承接动作：由 ActionJudge + Validator 判定，再交给 Narrator 护栏 ---
    st_o = GameState(l1, l1_start)
    st_o.add_player("测试员", {})
    fp_o = FakeProvider([
        _reply(tool_call("finish", {"narration": "你捡来枯枝生火，火堆很快燃了起来。"})),
    ])
    ctx_o = NarratorContext(state=st_o, player_name="测试员")
    blocked_plan = validate_action(
        l1.get_scene(l1_start), "我生火取暖",
        ActionJudgement(intent="blocked", confidence=0.95, refusal=True), False,
    )
    check("行动裁定：无机制动作被标为 blocked",
          blocked_plan.intent == "blocked", str(blocked_plan))
    # Narrator 单元测试直接接收上游裁定结果；真实房间路径由 on_action 设置该字段。
    ctx_o.oob_verb = "我生火取暖"
    res_o = await NarratorAgent(l1, fp_o).run(ctx_o, "我生火取暖")
    check("不可承接动作：Narrator 接收上游护栏", bool(ctx_o.oob_verb), ctx_o.oob_verb)
    msgs = NarratorAgent(l1, None).init_messages(ctx_o, "我生火取暖")
    check("越界：user 消息带硬指令（不得描写成功）",
          "越界动作" in msgs[-1]["content"] and "不得描写这个动作成功" in msgs[-1]["content"],
          msgs[-1]["content"][-120:])
    check("不可承接动作：旁白未明确拒绝 → 丢掉",
          res_o.text == "" and "当前玩法不支持" in getattr(res_o, "drop_reason", ""),
          f"text={res_o.text[:30]!r} reason={getattr(res_o, 'drop_reason', '')}")
    # 命中剧本声明的检定 id 时由 Validator 接受，不依赖通用关键词表。
    accepted_plan = validate_action(
        l1.get_scene("guard_tomb"), "我砸开石像看看",
        ActionJudgement(intent="check", check_id="smash_statue", confidence=0.95), False,
    )
    check("行动裁定：声明的检定动作可承接",
          accepted_plan.intent == "check" and accepted_plan.check_id == "smash_statue",
          str(accepted_plan))
    # 旁白提到越界动词但没有成功断言（合法的拒绝写法）不丢
    st_o2 = GameState(l1, l1_start)
    st_o2.add_player("测试员", {})
    fp_o2 = FakeProvider([
        _reply(tool_call("finish", {"narration": "这里太潮了，生火是不可能的，你只能继续往前。"})),
    ])
    res_o2 = await NarratorAgent(l1, fp_o2).run(
        NarratorContext(state=st_o2, player_name="测试员"), "我生火")
    check("越界：正确的拒绝写法不会被丢",
          res_o2.text != "" and not getattr(res_o2, "drop_reason", ""), res_o2.text[:30])

    # 清理
    for f in (jsonl, tmp / "c.meta.json", tmp / "v.jsonl", tmp / "v.meta.json",
              tmp / "g.jsonl", tmp / "g.meta.json"):
        if f.exists():
            f.unlink()
    try:
        tmp.rmdir()
    except OSError:
        pass


def http_base():
    ws_url = os.environ.get("RPGBAR_WS_URL", "ws://127.0.0.1:8000/ws")
    return ws_url.replace("ws://", "http://").replace("wss://", "https://").rsplit("/ws", 1)[0]

def ws_url():
    return os.environ.get("RPGBAR_WS_URL", "ws://127.0.0.1:8000/ws")


async def server_script_name():
    """读取服务器当前加载的剧本名，让实时校验能适配不同剧本。"""
    try:
        import httpx

        return httpx.get(http_base() + "/api/version", timeout=8, trust_env=False).json().get("script", "")
    except Exception:
        return ""


# 每种剧本一套实时校验动作：都用「确定性、不依赖骰子成败」的那几步
LIVE_SCRIPTS = [
    ("古堡秘宝", {"move": "推开铁门进入门厅", "scene": "hall", "check": "调查挂毯"}),
    ("蛇王墓", {"move": "钻进石门，走进甬道", "scene": "entrance_hall", "check": "观察甬道"}),
]


def live_actions(script_name):
    for key, val in LIVE_SCRIPTS:
        if key in (script_name or ""):
            return val
    return LIVE_SCRIPTS[0][1]


_TIMEOUT_SCALE = float(os.environ.get("RPGBAR_TEST_TIMEOUT_SCALE", "1") or "1")
"""联机测试的超时倍数。

默认 1（即 6 / 8 秒），对云 API 与本机 llama.cpp 这类秒级返回的通道够用。
**内置引擎是纯 CPU 推理**：实测 1.7B 生成一条行动建议约 96 秒、DM 一轮旁白数十秒，
远超默认值 —— 那样测出来的 `[FAIL]` 是"等不到"而不是"功能坏"。
跑引擎配置时把倍数放宽，例如：

    RPGBAR_TEST_TIMEOUT_SCALE=30 python scripts/smoke_test.py
"""


async def recv_until(ws, pred, timeout=6.0):
    end = time.time() + timeout * _TIMEOUT_SCALE
    got = None
    while time.time() < end:
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time()))
        except asyncio.TimeoutError:
            break
        if pred(msg):
            return msg
        got = msg
    return got


async def drain_pending(ws, budget=0.05, limit=80):
    """把**已经排队**的消息读干净。

    房间事件是广播的（DM 的 `thinking` 是房间级状态，见 main.py 的 `broadcast`），
    所以别的玩家推进场景时塞进本连接的那些 `thinking(dm, active=True)` 会一直躺在
    socket 里。若不断言前先清掉，下面 `recv_until(thinking)` 拿到的就是**上一轮**的
    旧事件 —— 断言"本轮请求先广播了什么"必须先清场。

    （小助手的 `thinking` 是 `send_to` 私发给请求者的，不会串台；会被串的只有 DM 的。）
    """
    for _ in range(limit):
        try:
            await asyncio.wait_for(ws.recv(), timeout=budget)
        except Exception:  # noqa: BLE001  TimeoutError / 连接关闭都算"清完了"
            break


async def start_room(ws, timeout=6.0):
    """房主点「开始游戏」：等待中 → 进行中。返回是否真的翻转了。

    2026-10-14 起房间先是**等待中**（大厅 + 等待室），开局前行动会被阶段门挡下
    （回 `not_started`）。所以凡是「加入后马上行动」的用例都得先走这一步 ——
    不然测到的是「被挡住了」，而不是「行动生效了」。
    """
    await ws.send(json.dumps({"type": "start"}, ensure_ascii=False))
    snap = await recv_until(
        ws,
        lambda m: m["type"] == "room" and (m.get("room") or {}).get("phase") == "playing",
        timeout=timeout,
    )
    return bool(snap)


async def run_live():
    print("== 实时联机校验（需服务器已启动） ==")
    try:
        import websockets
    except ImportError:
        print("  [SKIP] 未安装 websockets，跳过实时校验")
        return

    room = "TEST1"
    url = ws_url()
    acts = live_actions(await server_script_name())
    try:
        a = await websockets.connect(url, proxy=None)
        b = await websockets.connect(url, proxy=None)
    except Exception as e:
        print(f"  [SKIP] 无法连接服务器 {url}：{e}")
        return

    try:
        await a.send(json.dumps({"type": "join", "name": "亚瑟", "room": room, "character": {"cls": "战士"}}, ensure_ascii=False))
        await b.send(json.dumps({"type": "join", "name": "梅林", "room": room, "character": {"cls": "法师"}}, ensure_ascii=False))

        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        wb = await recv_until(b, lambda m: m["type"] == "welcome")
        check("双客户端拿到 welcome", wa and wb, f"{bool(wa)}/{bool(wb)}")
        check("welcome 带回剧本名", bool(wa) and bool(wa.get("script")), str(wa))
        check(
            "联机：刚建的房间是等待中（等房主开始，开局前不能行动）",
            ((wa or {}).get("room_info") or {}).get("phase") == "waiting",
            str((wa or {}).get("room_info")),
        )
        check("联机：房主点「开始游戏」后进入进行中", await start_room(a))
        await drain_pending(a)
        await drain_pending(b)

        # 亚瑟推进场景（出口推进，无骰子，确定性）
        await a.send(json.dumps({"type": "action", "text": acts["move"]}, ensure_ascii=False))
        st = await recv_until(a, lambda m: m["type"] == "state" and m["state"]["current_scene"] == acts["scene"])
        # ⚠️ 断言里要**再判一次场景**：`recv_until` 在等不到匹配时会返回"最后一条读到的消息"，
        # 只判 `bool(st)` 的话，只要收到过任何消息就算过 —— 实测正是这条把「行动被阶段门挡下、
        # 场景压根没动」这种真故障掩盖成了 PASS。
        check(
            "联机：出口推进到下一场景",
            bool(st) and (st.get("state") or {}).get("current_scene") == acts["scene"],
            f"未到达 {acts['scene']}：{str(st)[:80]}",
        )

        # 亚瑟触发检定（必有 dice 事件）
        await a.send(json.dumps({"type": "action", "text": acts["check"]}, ensure_ascii=False))
        dice_ev = await recv_until(a, lambda m: m["type"] == "dice")
        check("联机：检定触发骰子事件", bool(dice_ev), "无 dice 事件")

        # 梅林请求助手建议
        # 先清掉房间广播塞进来的旧事件（亚瑟那两步的 thinking(dm) 都还在 b 的队列里），
        # 否则下面拿到的会是「亚瑟行动时的 DM 思考」而不是「梅林请求的助手思考」。
        await drain_pending(b)
        await b.send(json.dumps({"type": "suggest"}, ensure_ascii=False))
        th = await recv_until(
            b,
            lambda m: m["type"] == "thinking" and m.get("who") == "advisor" and m.get("active") is True,
        )
        check("联机：请求建议会先广播「助手思考中」", bool(th), str(th)[:80])
        sug = await recv_until(b, lambda m: m["type"] == "suggestions")
        check("联机：助手建议返回", bool(sug) and len(sug.get("options", [])) > 0, str(sug)[:80])

        # 梅林自由发挥（触发 DM 旁白）
        await drain_pending(b)
        await b.send(json.dumps({"type": "action", "text": "我环顾四周，警惕地观察"}, ensure_ascii=False))
        th2 = await recv_until(
            b,
            lambda m: m["type"] == "thinking" and m.get("who") == "dm" and m.get("active") is True,
        )
        check("联机：行动会先广播「DM 推理中」", bool(th2), str(th2)[:80])
        nar = await recv_until(b, lambda m: m["type"] == "narration")
        check("联机：自由发挥触发 DM 旁白", bool(nar), "无 narration")
    finally:
        await a.close()
        await b.close()


async def run_live_version():
    """校验 /api/version 与 /api/net（版本号与联机地址是发版机制/联机的一环）。"""
    import httpx

    try:
        r = httpx.get(http_base() + "/api/version", timeout=8, trust_env=False)
        data = r.json()
        check("联机：/api/version 返回版本号", r.status_code == 200 and bool(data.get("version")), str(data))
        check("联机：/api/version 返回剧本名", bool(data.get("script")), str(data))
    except Exception as e:
        print(f"  [SKIP] /api/version 不可用：{e}")

    try:
        r = httpx.get(http_base() + "/api/net", timeout=8, trust_env=False)
        data = r.json()
        addrs = data.get("addresses") or []
        check("联机：/api/net 返回地址列表", r.status_code == 200 and len(addrs) >= 1, str(data))
        check(
            "联机：/api/net 每项都带可分享 URL",
            all(a.get("url", "").startswith("http://") and a.get("ip") for a in addrs),
            str(addrs),
        )
        check("联机：/api/net 不含回环地址", all(not a["ip"].startswith("127.") for a in addrs), str(addrs))
    except Exception as e:
        print(f"  [SKIP] /api/net 不可用：{e}")


async def collect(ws, count, timeout=8.0):
    """按顺序收集若干条消息（不丢弃中间消息，便于断言顺序）。"""
    msgs = []
    end = time.time() + timeout * _TIMEOUT_SCALE
    while len(msgs) < count and time.time() < end:
        try:
            msgs.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time())))
        except (asyncio.TimeoutError, Exception):
            break
    return msgs


async def run_live_latejoin():
    print("== 实时中途加入校验（需服务器已启动） ==")
    try:
        import websockets
    except ImportError:
        print("  [SKIP] 未安装 websockets，跳过")
        return

    url = ws_url()
    acts = live_actions(await server_script_name())
    # 独立房间码，保证「先到者」进的是全新房间（否则会误判为中途加入）
    room = "LATE" + str(int(time.time() * 1000) % 100000)
    try:
        a = await websockets.connect(url, proxy=None)
    except Exception as e:
        print(f"  [SKIP] 无法连接服务器 {url}：{e}")
        return

    b = None
    try:
        await a.send(json.dumps({"type": "join", "name": "先到者", "room": room, "character": {"cls": "战士"}}, ensure_ascii=False))
        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        check("联机：首人加入 welcome.late=False", bool(wa) and wa.get("late") is False, str(wa))

        # 先让房主开局：房间先是「等待中」，不点开始的话下面每一步行动都会被阶段门挡下，
        # 而「中途加入」的判据也正是「此刻房间是不是进行中」—— 不开局就测不到补课。
        check("联机：房主开局后房间转为进行中", await start_room(a))

        # 先到者推进剧情
        await a.send(json.dumps({"type": "action", "text": acts["move"]}, ensure_ascii=False))
        st = await recv_until(a, lambda m: m["type"] == "state" and m["state"]["current_scene"] == acts["scene"])
        check(
            "联机：先到者推进场景",
            bool(st) and (st.get("state") or {}).get("current_scene") == acts["scene"],
            f"未到达 {acts['scene']}：{str(st)[:80]}",
        )
        await a.send(json.dumps({"type": "action", "text": acts["check"]}, ensure_ascii=False))
        await recv_until(a, lambda m: m["type"] == "dice")

        # 新玩家中途加入同一房间
        b = await websockets.connect(url, proxy=None)
        await b.send(json.dumps({"type": "join", "name": "迟到者", "room": room, "character": {"cls": "法师"}}, ensure_ascii=False))
        # 8 条：welcome / system / thinking(true) / narration / thinking(false) / recap / room / state —
        # 取 6 条会看不到 room 与 state（那条 state 断言就会退化成"空也过"）。
        b_msgs = await collect(b, 8)
        types = [m["type"] for m in b_msgs]

        wb = next((m for m in b_msgs if m["type"] == "welcome"), None)
        check("联机：中途加入 welcome.late=True", bool(wb) and wb.get("late") is True, str(wb))
        check("联机：welcome 先于补课消息", bool(types) and types[0] == "welcome", str(types))

        cap = next((m for m in b_msgs if m["type"] == "recap"), None)
        check("联机：新玩家收到私有故事回顾", bool(cap), str(types))
        rc = (cap or {}).get("recap", {})
        check("联机：回顾含已走过的行程", len(rc.get("scene_path") or []) >= 2, str(rc.get("scene_path")))

        st_msg = next((m for m in b_msgs if m["type"] == "state"), None)
        st_state = (st_msg or {}).get("state") or {}
        fd = st_state.get("flag_details", [])
        check(
            "联机：状态快照带线索中文描述",
            # 先要求 state 真的到了：只判 `fd` 的话，收不到 state 时 fd 也是空列表、
            # 而 `all(空)` 为真 —— 一条本该验"线索描述"的断言会变成"空也过"。
            bool(st_msg)
            and isinstance(fd, list)
            and len(fd) == len(st_state.get("flags", []))
            and all(d.get("label") for d in fd),
            f"flags={st_state.get('flags')} details={str(fd)[:80]}",
        )

        intro = next((m for m in b_msgs if m["type"] == "narration"), None)
        check(
            "联机：DM 给出带入新人的旁白",
            bool(intro) and ("迟到者" in intro.get("text", "") or "法师" in intro.get("text", "")),
            str(intro)[:100],
        )

        # 老玩家也应看到「中场加入」广播与同一段旁白
        # 先清掉上一步 `check` 行动没被读完的尾巴（narration / thinking(false) / state / room），
        # 否则下面的 `collect(a, 6)` 会把它们当成"新消息"数进去，断言就是撞运气。
        await drain_pending(a)
        a_msgs = await collect(a, 6)
        check(
            "联机：老玩家收到中场加入广播",
            any(m["type"] == "system" and "中途加入" in m.get("text", "") for m in a_msgs),
            str([m["type"] for m in a_msgs]),
        )
        check(
            "联机：老玩家看到引导旁白",
            any(m["type"] == "narration" for m in a_msgs),
            str([m["type"] for m in a_msgs]),
        )
    finally:
        for w in (a, b):
            if w is not None:
                try:
                    await w.close()
                except Exception:
                    pass


async def run_live_configure():
    """实时校验「游戏内接入模型」这条链路：房主权限、槽位清洗、key 不外泄。"""
    print("== 实时模型设置校验（需服务器已启动） ==")
    try:
        import websockets
    except ImportError:
        print("  [SKIP] 未安装 websockets，跳过")
        return

    url = ws_url()
    room = "CFG" + str(int(time.time() * 1000) % 100000)
    try:
        a = await websockets.connect(url, proxy=None)
    except Exception as e:
        print(f"  [SKIP] 无法连接服务器 {url}：{e}")
        return

    b = None
    try:
        await a.send(json.dumps({"type": "join", "name": "房主", "room": room, "character": {"cls": "战士"}}, ensure_ascii=False))
        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        agents = (wa or {}).get("agents") or {}
        check("联机：welcome 带两个模型槽位状态", "dm" in agents and "advisor" in agents, str(agents)[:90])
        check("联机：welcome 标出房主", agents.get("owner") == "房主", str(agents.get("owner")))
        check(
            "联机：槽位状态不回传 api_key（只留布尔）",
            all("api_key" not in (agents.get(k) or {}) for k in ("dm", "advisor")),
            str(agents)[:90],
        )
        check("联机：默认留空即没有模型可用", agents.get("dm", {}).get("ready") is False, str(agents.get("dm"))[:90])

        # 房主把自己的小助手接到本地模型（本地不需要 key）
        await a.send(
            json.dumps(
                {"type": "configure", "advisor": {"kind": "local", "base_url": "http://127.0.0.1:8080/v1", "model": "qwen2.5:3b"}},
                ensure_ascii=False,
            )
        )
        st = await recv_until(a, lambda m: m["type"] == "agent_status")
        adv = ((st or {}).get("agents") or {}).get("advisor") or {}
        check("联机：小助手可接入本地模型", adv.get("ready") is True and adv.get("kind") == "local", str(adv)[:110])
        check("联机：agent_status 标出 changed=advisor", (st or {}).get("changed") == ["advisor"], str((st or {}).get("changed")))

        # 房主把主机 DM 接到云 API
        await a.send(
            json.dumps(
                {
                    "type": "configure",
                    "dm": {"kind": "cloud", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key": "sk-smoketest"},
                },
                ensure_ascii=False,
            )
        )
        st2 = await recv_until(a, lambda m: m["type"] == "agent_status" and "dm" in (m.get("changed") or []))
        dm = ((st2 or {}).get("agents") or {}).get("dm") or {}
        check("联机：房主可改主机 DM", dm.get("ready") is True and dm.get("has_api_key") is True, str(dm)[:110])
        check("联机：回传不含明文 key", "sk-smoketest" not in json.dumps(st2, ensure_ascii=False), str(st2)[:80])

        # 非房主改 DM → 被拒
        b = await websockets.connect(url, proxy=None)
        await b.send(json.dumps({"type": "join", "name": "路人", "room": room, "character": {"cls": "法师"}}, ensure_ascii=False))
        wb = await recv_until(b, lambda m: m["type"] == "welcome")
        check("联机：第二人不算房主", ((wb or {}).get("agents") or {}).get("owner") == "房主", str(((wb or {}).get("agents") or {}).get("owner")))
        await b.send(json.dumps({"type": "configure", "dm": {"kind": "cloud", "base_url": "https://x/v1", "model": "m", "api_key": "sk-evil"}}, ensure_ascii=False))
        err = await recv_until(b, lambda m: m["type"] in ("error", "agent_status"))
        check("联机：非房主改 DM 被拒", (err or {}).get("type") == "error" and "房主" in (err or {}).get("message", ""), str(err)[:100])

        # 脏字段（非法 kind / 非 http 的 base_url / 白名单外字段）应被静默丢弃
        await b.send(
            json.dumps(
                {"type": "configure", "advisor": {"kind": "hacked", "base_url": "ftp://evil", "model": "m", "drop_table": True}},
                ensure_ascii=False,
            )
        )
        st3 = await recv_until(b, lambda m: m["type"] == "agent_status")
        adv3 = ((st3 or {}).get("agents") or {}).get("advisor") or {}
        check("联机：非法 kind 被忽略（仍为 off）", adv3.get("kind") == "off", str(adv3)[:110])
        check("联机：非 http 的 base_url 被丢弃", adv3.get("base_url") != "ftp://evil", str(adv3.get("base_url")))
    finally:
        for w in (a, b):
            if w is not None:
                try:
                    await w.close()
                except Exception:
                    pass


async def run_live_model_test():
    """实时校验 /api/models/test（设置界面「测试连接」按钮的后端）。"""
    print("== 实时模型探测接口校验（需服务器已启动） ==")
    try:
        import httpx

        r = httpx.post(
            http_base() + "/api/models/test",
            json={"slot": {"kind": "off"}},
            timeout=15,
            trust_env=False,
        )
        d = r.json()
        check("联机：测试接口拒绝空配置", r.status_code == 200 and d.get("ok") is False, str(d)[:120])
        check("联机：测试接口给出可读错误", "error" in d and bool(d["error"]), str(d)[:120])
    except Exception as e:
        print(f"  [SKIP] /api/models/test 不可用：{e}")


async def run_live_scripts():
    """实时校验剧本接口：列表 / 读取切片 / 路径越界拦截 / 切换活动剧本。"""
    print("== 实时剧本接口校验（需服务器已启动） ==")
    try:
        import httpx

        base = http_base()

        r = httpx.get(base + "/api/scripts", timeout=15, trust_env=False)
        d = r.json()
        check("联机：剧本列表接口可用", r.status_code == 200 and isinstance(d.get("scripts"), list), str(d)[:120])
        check("联机：列表含当前活动剧本", bool(d.get("active")) and bool(d.get("active_title")), str(d)[:120])
        names = [it.get("file") for it in d.get("scripts", [])]
        check("联机：列表能发现内置剧本", "sample_script.json" in names and "totsk_l1.json" in names, str(names))
        check("联机：列表项带切片数", all("chunks" in it for it in d.get("scripts", [])), str(d.get("scripts"))[:120])

        # 越界路径必须被拦下（服务端绑 0.0.0.0，不能变成任意文件读取）
        r = httpx.post(base + "/api/scripts/inspect", json={"path": "../../server/main.py"}, timeout=15, trust_env=False)
        check("联机：越界路径被拒绝", r.json().get("ok") is False, str(r.json())[:120])
        r = httpx.post(base + "/api/scripts/inspect", json={"path": "C:/Windows/win.ini"}, timeout=15, trust_env=False)
        check("联机：绝对路径被拒绝", r.json().get("ok") is False, str(r.json())[:120])

        # 正常读取：切片 + 两份次级 prompt
        r = httpx.post(base + "/api/scripts/inspect", json={"path": "totsk_l1.json"}, timeout=20, trust_env=False)
        d = r.json()
        check("联机：读取剧本成功", d.get("ok") is True and d.get("title"), str(d)[:120])
        check("联机：返回切片清单", len(d.get("chunks") or []) == 10, str(len(d.get("chunks") or [])))
        check("联机：返回 DM 次级 prompt", "【切片索引】" in (d.get("script_prompt") or ""), "")
        check("联机：返回小助手次级 prompt（无索引）",
              bool(d.get("script_prompt_public")) and "【切片索引】" not in d["script_prompt_public"], "")
        check("联机：次级 prompt 不含本机绝对路径",
              "E:\\" not in (d.get("script_prompt") or "") and "E:/" not in (d.get("script_prompt") or ""), "")

        # 粘贴内容也能切片
        r = httpx.post(
            base + "/api/scripts/inspect",
            json={"content": "# 序章\n渡船靠岸。\n\n# 尾声\n钟声响起。", "name": "临时剧本"},
            timeout=20,
            trust_env=False,
        )
        d = r.json()
        check("联机：粘贴内容也能切片", d.get("ok") is True and d.get("fmt") == "text" and len(d["chunks"]) == 2, str(d)[:120])

        # 切换活动剧本 → 版本接口跟着变 → 再切回来（不影响其它用例）
        before = httpx.get(base + "/api/version", timeout=15, trust_env=False).json()
        target = "sample_script.json" if before.get("script_path") != "sample_script.json" else "totsk_l1.json"
        r = httpx.post(base + "/api/scripts/load", json={"path": target}, timeout=25, trust_env=False)
        d = r.json()
        check("联机：切换活动剧本成功", d.get("ok") is True, str(d)[:160])
        after = httpx.get(base + "/api/version", timeout=15, trust_env=False).json()
        check("联机：切换后 /api/version 跟随更新", after.get("script_path") == target, f"{after.get('script_path')} != {target}")
        check("联机：切换后带自检信息", bool(after.get("script_info")), str(after.get("script_info"))[:120])

        r = httpx.post(base + "/api/scripts/load", json={"path": before.get("script_path")}, timeout=25, trust_env=False)
        check("联机：切回原剧本成功", r.json().get("ok") is True, str(r.json())[:120])
        r = httpx.post(base + "/api/scripts/load", json={"path": "不存在的剧本.json"}, timeout=15, trust_env=False)
        check("联机：载入不存在的剧本被拒", r.json().get("ok") is False, str(r.json())[:120])
    except Exception as e:
        print(f"  [SKIP] 剧本接口不可用：{e}")


async def run_live_room_script():
    """建房时选剧本 → 朋友连入自动同步剧本与两份 Agent prompt。"""
    print("== 实时房间剧本同步校验（需服务器已启动） ==")
    try:
        import websockets
    except ImportError:
        print("  [SKIP] 未安装 websockets，跳过")
        return

    import httpx

    try:
        listing = httpx.get(http_base() + "/api/scripts", timeout=8, trust_env=False).json()
    except Exception as e:
        print(f"  [SKIP] /api/scripts 不可用：{e}")
        return
    items = [s for s in listing.get("scripts", []) if not s.get("error")]
    if len(items) < 2:
        print("  [SKIP] 需要至少两个剧本才能验证「建房选剧本」")
        return
    active = listing.get("active")
    pick = next((s for s in items if s["path"] != active), items[0])

    url = ws_url()
    room = "SCR" + str(int(time.time() * 1000) % 100000)
    try:
        a = await websockets.connect(url, proxy=None)
    except Exception as e:
        print(f"  [SKIP] 无法连接服务器 {url}：{e}")
        return

    b = None
    try:
        await a.send(
            json.dumps(
                {"type": "join", "name": "房主", "room": room, "script": pick["path"], "character": {"cls": "战士"}},
                ensure_ascii=False,
            )
        )
        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        si = (wa or {}).get("script_info") or {}
        check("联机：建房时选中的剧本生效", si.get("path") == pick["path"], f"{si.get('path')} != {pick['path']}")
        chunks = (wa or {}).get("chunks") or []
        check("联机：welcome 同步切片清单", len(chunks) == si.get("chunks"), f"{len(chunks)} vs {si.get('chunks')}")
        prompt = (wa or {}).get("prompt") or {}
        check("联机：房主收到 DM prompt", bool(prompt.get("dm")))
        check("联机：所有人收到小助手 prompt", bool(prompt.get("advisor")))
        check("联机：DM 与小助手 prompt 不相同", prompt.get("dm") != prompt.get("advisor"))

        # 朋友用同一个房间码加入（不带剧本）→ 应自动同步到房主选的剧本
        b = await websockets.connect(url, proxy=None)
        await b.send(
            json.dumps({"type": "join", "name": "朋友", "room": room, "character": {"cls": "法师"}}, ensure_ascii=False)
        )
        b_msgs = await collect(b, 4)
        wb = next((m for m in b_msgs if m["type"] == "welcome"), None)
        sb = (wb or {}).get("script_info") or {}
        check("联机：朋友加入后同步到房主选的剧本", sb.get("path") == pick["path"], str(sb.get("path")))
        pb = (wb or {}).get("prompt") or {}
        check("联机：非房主拿不到 DM prompt", pb.get("dm") in (None, ""), str(pb.get("dm"))[:40])
        check("联机：非房主仍拿到小助手 prompt", bool(pb.get("advisor")))
    except Exception as e:  # noqa: BLE001
        print(f"  [ERROR] {type(e).__name__}: {e}")
    finally:
        for conn in (b, a):
            try:
                if conn is not None:
                    await conn.close()
            except Exception:
                pass


async def run_live_npc():
    """实时：NPC 子系统在房间里的接线（无模型也跑得通：原型兜底 + 骰子采样）。"""
    print("== 实时校验：NPC 子系统（人物卡同步 / 与 NPC 互动不崩） ==")
    try:
        import websockets
    except ImportError:
        print("  [SKIP] 未安装 websockets，跳过实时校验")
        return
    room = "NPC" + str(int(time.time()))[-4:]
    url = ws_url()
    try:
        a = await websockets.connect(url, proxy=None)
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] 无法连接服务器 {url}：{e}")
        return
    try:
        await a.send(json.dumps({"type": "join", "name": "甲", "room": room, "character": {"cls": "战士"}}, ensure_ascii=False))
        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        npcs = (wa or {}).get("npcs") or []
        # 断言「房间的 NPC 列表被同步下来了、每张卡都有名字」——不写死具体名字：
        # NPC 来自**当前活动剧本**（sample_script 是老周、蛇王墓是卡特·维恩……），
        # 写死一个名字等于把这条断言绑在某个剧本上。
        check("NPC 实时：welcome 同步本房间 NPC 列表",
              bool(npcs) and all(n.get("name") for n in npcs), str(npcs)[:140])
        check("NPC 实时：NPC 卡带身份", any(n.get("role") for n in npcs), str(npcs)[:140])

        # 房间先是「等待中」，得先让房主开局，否则下面的互动会被阶段门挡下、
        # 拿到的只有一条 `not_started`（看着像"NPC 互动没产出旁白"）。
        check("NPC 实时：房主开局后房间转为进行中", await start_room(a))
        await drain_pending(a)

        # 互动对象取本房间第一张人物卡（同样不写死名字）。
        npc_name = npcs[0]["name"] if npcs else "路人"
        await a.send(json.dumps({"type": "action", "text": f"我盯着{npc_name}看"}, ensure_ascii=False))
        got, deadline = [], time.time() + 10
        while time.time() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(a.recv(), timeout=max(0.1, deadline - time.time())))
            except asyncio.TimeoutError:
                break
            got.append(m)
            if m.get("type") == "state":
                break
        check("NPC 实时：与 NPC 互动过程无 error", not any(m.get("type") == "error" for m in got), str(got)[:160])
        check("NPC 实时：互动产出了旁白/骰子", any(m.get("type") in ("narration", "dice") for m in got), str([m.get("type") for m in got]))
        st_npcs = (got[-1].get("state", {}).get("npcs") if got and got[-1].get("type") == "state" else None)
        check("NPC 实时：state 持续携带 NPC 列表", isinstance(st_npcs, list) and len(st_npcs) >= 1, str(st_npcs)[:140])
    except Exception as e:  # noqa: BLE001
        print(f"  [ERROR] {type(e).__name__}: {e}")
    finally:
        try:
            await a.close()
        except Exception:
            pass


async def run_live_chronicle():
    """实时：剧情档案在房间里真的在记（welcome.state 带 memory、行动后条目数增长）。"""
    print("== 实时校验：剧情档案（welcome 状态 / 行动后落盘） ==")
    try:
        import websockets
    except ImportError:
        print("  [SKIP] 未安装 websockets，跳过实时校验")
        return
    room = "CHR" + str(int(time.time()))[-4:]
    url = ws_url()
    try:
        a = await websockets.connect(url, proxy=None)
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] 无法连接服务器 {url}：{e}")
        return
    try:
        await a.send(json.dumps({"type": "join", "name": "甲", "room": room, "character": {"cls": "游侠"}}, ensure_ascii=False))
        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        mem = (wa or {}).get("memory") or {}
        check("档案实时：version/欢迎包带档案状态", isinstance(mem, dict) and "entries" in mem, str(mem)[:140])

        # 同样要先开局：等待阶段行动被挡，档案条目自然一条都不涨。
        check("档案实时：房主开局后房间转为进行中", await start_room(a))
        await drain_pending(a)

        await a.send(json.dumps({"type": "action", "text": "我打量四周并记下来"}, ensure_ascii=False))
        got, deadline = [], time.time() + 10
        while time.time() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(a.recv(), timeout=max(0.1, deadline - time.time())))
            except asyncio.TimeoutError:
                break
            got.append(m)
            if m.get("type") == "state":
                break
        check("档案实时：行动过程无 error", not any(m.get("type") == "error" for m in got), str(got)[:160])
        st = got[-1].get("state", {}) if got and got[-1].get("type") == "state" else {}
        mem2 = st.get("memory") or {}
        check("档案实时：state 携带档案状态", isinstance(mem2, dict) and "entries" in mem2, str(mem2)[:140])
        base = int(mem.get("entries") or 0)
        now = int(mem2.get("entries") or 0)
        check("档案实时：行动后档案条目数增长", now > base, f"{base} -> {now}")
        check("档案实时：状态标出是否启用嵌入模型", "embedding" in mem2, str(mem2)[:140])
    except Exception as e:  # noqa: BLE001
        print(f"  [ERROR] {type(e).__name__}: {e}")
    finally:
        try:
            await a.close()
        except Exception:
            pass


async def run_unit_engine():
    """内置引擎的接入层。

    刻意**不加载真实的 dll 与权重**（那样每个跑测试的机器都要先下 1.8 GB），
    只验那些"坏了会静默出错"的形状与退化逻辑：路径找不到时不回退、引擎没启用时
    槽位自动退化、工具调用形状转换对得上 `agents/base.py` 的消费方式。
    """
    print("\n== 内置引擎接入（不加载真实 dll）==")
    from server import engine as engine_mod
    from server.config import DEFAULTS, _apply_engine_slots
    from server.llm import EngineProvider, _to_openai_calls, build_provider

    # 1. 写了路径却找不到 → 返回 None，不偷偷换一个能加载的
    check("引擎：显式路径不存在时不回退",
          engine_mod.find_lib("Z:/nope/rpg_engine.dll") is None
          and engine_mod.find_model("Z:/nope/nope.gguf") is None)
    check("引擎：库或权重缺一就不建 Provider（静默退化）",
          build_provider({"kind": "engine", "lib": "Z:/nope.dll",
                          "model": "Z:/nope.gguf"}) is None)
    check("引擎：kind=off 仍然返回 None（老行为不变）",
          build_provider({"kind": "off"}) is None and build_provider(None) is None)

    # 1.5 推理后端（CPU / GPU）：在文件层面就是"找哪一份库"。
    #     卡片上那个下拉框只能在这张**枚举好的名单**里挑（客户端指不了任意 dll），
    #     所以判据是两条：名字表对不对、找不到 GPU 库时的退回规则对不对。
    import tempfile

    check("引擎：后端只有 cpu / gpu 两个值", engine_mod.BACKENDS == ("cpu", "gpu"))
    check("引擎：GPU 版产物名带 _cuda（文件名即后端）",
          all("_cuda" in n for n in engine_mod._lib_filenames("gpu"))
          and not any("_cuda" in n for n in engine_mod._lib_filenames("cpu")),
          str(engine_mod._lib_filenames("gpu")))
    check("引擎：后端别名收敛（cuda→gpu / host→cpu / 认不出→默认）",
          engine_mod.normalize_backend("cuda") == "gpu"
          and engine_mod.normalize_backend("GPU") == "gpu"
          and engine_mod.normalize_backend("host") == "cpu"
          and engine_mod.normalize_backend("amd") == "cpu"
          and engine_mod.normalize_backend("", default="") == "")

    # 找库的顺序用一个临时目录验（不依赖本机装了哪个后端）
    with tempfile.TemporaryDirectory() as _td:
        _d = Path(_td)
        _cpu_name = engine_mod._lib_filenames("cpu")[0]
        _gpu_name = engine_mod._lib_filenames("gpu")[0]
        _real_dirs = engine_mod._search_dirs
        engine_mod._search_dirs = lambda: [_d]
        try:
            (_d / _cpu_name).write_bytes(b"fake")
            check("引擎：只要 CPU 时不去碰 CUDA 产物",
                  engine_mod.find_lib("", "cpu") == _d / _cpu_name)
            check("引擎：**GPU 库缺失时退回 CPU**（不是退化成没引擎）",
                  engine_mod.find_lib("", "gpu") == _d / _cpu_name
                  and engine_mod.lib_backend_hint(engine_mod.find_lib("", "gpu")) == "cpu")
            (_d / _gpu_name).write_bytes(b"fake")
            check("引擎：有 CUDA 产物时 gpu 选它",
                  engine_mod.find_lib("", "gpu") == _d / _gpu_name
                  and engine_mod.lib_backend_hint(engine_mod.find_lib("", "gpu")) == "gpu")
            check("引擎：cpu 仍然只认 CPU 产物（有 CUDA 库也不换）",
                  engine_mod.find_lib("", "cpu") == _d / _cpu_name)
        finally:
            engine_mod._search_dirs = _real_dirs
    check("引擎：显式写了库路径就不按后端换（指错了就是没有）",
          engine_mod.find_lib("Z:/nope/rpg_engine_cuda.dll", "gpu") is None
          and engine_mod.find_lib("Z:/nope/rpg_engine.dll", "cpu") is None)

    # 实际后端以引擎自己报的为准（CUDA 版多返回 engine / backend 两个键 —— 不改 ABI）
    from server.engine import backend_from_info

    check("引擎：实际后端以 model_info 为准（CUDA 版多两个键）",
          backend_from_info({"engine": "rpg_engine_cuda", "backend": "cuda"}) == "gpu"
          and backend_from_info({"backend": "host-only"}) == "host-only"
          and backend_from_info({"arch": "qwen3", "vocab": 151936}) == "cpu"
          and backend_from_info(None) == "",
          str(backend_from_info({"engine": "rpg_engine_cuda"})))

    # 2. 配置：engine.enabled=false 时，kind=engine 的槽位退化为 off
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["models"]["assistant"]["kind"] = "engine"
    _apply_engine_slots(cfg)
    check("引擎：未启用时 engine 槽位退化为 off",
          cfg["models"]["assistant"]["kind"] == "off")

    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["engine"].update({"enabled": True, "context": 3072, "think": False})
    cfg["models"]["advisor"]["kind"] = "engine"
    _apply_engine_slots(cfg)
    slot = cfg["models"]["advisor"]
    check("引擎：启用后把 engine 段供货给槽位",
          slot["kind"] == "engine" and slot["context"] == 3072
          and slot["think"] is False and "lib" in slot)
    cfg["models"]["advisor"]["context"] = 512
    _apply_engine_slots(cfg)
    check("引擎：槽位自己写了的字段优先于 engine 段",
          cfg["models"]["advisor"]["context"] == 512)

    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["engine"].update({"enabled": True, "backend": "gpu"})
    cfg["models"]["dm"]["kind"] = "engine"
    _apply_engine_slots(cfg)
    check("引擎：engine.backend 也供货给槽位（界面那个下拉的落点）",
          cfg["models"]["dm"]["backend"] == "gpu", repr(cfg["models"]["dm"].get("backend")))

    # 2.5 槽位 `model` 的供货（README 推荐写法的实现处）
    # 槽位 DEFAULTS 里的 model 是云 / 本地通道的**模型名**（deepseek-chat 等），
    # 在 engine 下没有意义 —— 判据必须是"这个值说不说得通"，不是"槽位有没有这个键"。
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["engine"].update({"enabled": True, "model": "D:/weights/Qwen3-1.7B.gguf"})
    cfg["models"]["dm"]["kind"] = "engine"
    _apply_engine_slots(cfg)
    check("引擎：engine.model 顶替槽位的云模型名默认值",
          cfg["models"]["dm"]["model"] == "D:/weights/Qwen3-1.7B.gguf",
          repr(cfg["models"]["dm"]["model"]))

    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["engine"].update({"enabled": True, "model": "D:/weights/default.gguf"})
    cfg["models"]["dm"].update(
        {"kind": "engine", "model": "D:/weights/Qwen3-4B.gguf"})
    _apply_engine_slots(cfg)
    check("引擎：槽位显式写的权重路径优先于 engine 段",
          cfg["models"]["dm"]["model"] == "D:/weights/Qwen3-4B.gguf",
          repr(cfg["models"]["dm"]["model"]))

    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["engine"].update({"enabled": True, "model": ""})
    cfg["models"]["dm"]["kind"] = "engine"
    _apply_engine_slots(cfg)
    check("引擎：engine.model 留空时槽位也清空（触发自动探测）",
          cfg["models"]["dm"]["model"] == "",
          repr(cfg["models"]["dm"]["model"]))

    # 2.6 界面上的「内置引擎」开关（设置抽屉里那一段）：白名单 + 房间级合并
    from server.main import Room, clean_engine_patch

    dirty = clean_engine_patch({
        "enabled": True, "think": True,
        "lib": "C:/evil.dll", "model": "C:/evil.gguf",   # 不在白名单 → 必须丢掉
        "context": 999999, "max_tokens": 1, "junk": 1,
    })
    check("引擎：界面补丁只收白名单字段（lib / model 进不来）",
          "lib" not in dirty and "model" not in dirty and "junk" not in dirty,
          str(dirty))
    check("引擎：界面补丁把 context / max_tokens 钳进合理区间",
          dirty["context"] == 32768 and dirty["max_tokens"] == 16, str(dirty))
    check("引擎：界面补丁接受布尔开关",
          dirty["enabled"] is True and dirty["think"] is True, str(dirty))
    check("引擎：界面补丁只收 cpu / gpu 两个后端值",
          clean_engine_patch({"backend": "gpu"}) == {"backend": "gpu"}
          and clean_engine_patch({"backend": "cuda"}) == {"backend": "gpu"}
          and clean_engine_patch({"backend": "amd"}) == {}
          and clean_engine_patch({"backend": 3}) == {},
          str(clean_engine_patch({"backend": "amd"})))
    check("引擎：界面补丁对垃圾输入返回空",
          clean_engine_patch(None) == {} and clean_engine_patch("x") == {})

    # 合并语义：**槽位显式值优先**，引擎段只补缺省；不改动原槽位（它还要下发前端）
    room = Room.__new__(Room)   # 绕过 __init__：这条断言只碰 _effective_slot
    room.engine_slot = {"enabled": True, "think": True, "context": 8192,
                        "max_tokens": 256, "backend": "gpu"}
    dm = {"kind": "engine", "think": False, "context": 1024, "model": "D:/w.gguf"}
    eff = room._effective_slot(dm)
    check("引擎：槽位显式写的字段优先于引擎段（按槽位分后端/上下文）",
          eff["think"] is False and eff["context"] == 1024, str(eff))
    check("引擎：槽位没写的字段用引擎段缺省",
          eff["max_tokens"] == 256 and eff["backend"] == "gpu", str(eff))
    check("引擎：合并不改动原槽位",
          dm["think"] is False and dm["context"] == 1024, str(dm))
    eff_cpu = room._effective_slot({"kind": "engine", "backend": "cpu", "context": 2048})
    check("引擎：槽位可以单独指定 CPU 后端（DM 走 GPU、助手走 CPU 的前提）",
          eff_cpu["backend"] == "cpu" and eff_cpu["context"] == 2048
          and eff_cpu["max_tokens"] == 256, str(eff_cpu))
    check("引擎：非 engine 槽位原样返回",
          room._effective_slot({"kind": "cloud"}) == {"kind": "cloud"})
    room.engine_slot = {"enabled": False}
    check("引擎：界面关掉引擎后该槽位视为没模型",
          room._effective_slot({"kind": "engine"}) is None)
    check("引擎：总开关关掉时，槽位写 enabled=True 也按关闭处理（不能按回去）",
          room._effective_slot({"kind": "engine", "enabled": True}) is None)

    # 3. 槽位公开状态：带 engine 详情，且**不下发绝对路径**
    pub = describe_agents(
        {"kind": "off"}, {"kind": "engine", "model": "D:/somewhere/Qwen3-1.7B.gguf"}
    )["advisor"]
    check("引擎：公开状态带 engine 详情",
          isinstance(pub.get("engine"), dict) and "lib_ok" in pub["engine"]
          and "ready" in pub["engine"])
    check("引擎：公开状态报出请求后端 / 实际库 / 是否退回",
          pub["engine"]["backend"] == "cpu"
          and "lib_backend" in pub["engine"]
          and isinstance(pub["engine"]["backend_fallback"], bool),
          json.dumps(pub["engine"], ensure_ascii=False))
    _pub_gpu = describe_agents(
        {"kind": "off"}, {"kind": "engine", "backend": "gpu", "model": "Q.gguf"}
    )["advisor"]["engine"]
    check("引擎：公开状态带得动 gpu 请求（界面据此标'实际 CPU'）",
          _pub_gpu["backend"] == "gpu"
          and _pub_gpu["backend_fallback"] == (_pub_gpu["lib_backend"] == "cpu"),
          json.dumps(_pub_gpu, ensure_ascii=False))
    check("引擎：公开状态只给权重文件名，不给本机路径",
          pub["model"] == "Qwen3-1.7B.gguf", str(pub.get("model")))

    # 1.6 GPU 可用性探测：**"产物在" ≠ "能用"** —— CUDA 版要随包分发（对方机器
    #     没独显也得有文件），所以界面上的「GPU」置灰必须看探测结果，不能看文件名。
    #     三道门：产物在 → 能不能加载 → 驱动报得出设备（**不是**问 cudart：
    #     实测 CUDA 版把 cudart 静态链进去了，宿主侧根本没有 cudaGetDeviceCount）。
    with tempfile.TemporaryDirectory() as _td:
        _d = Path(_td)
        _gpu_name = engine_mod._lib_filenames("gpu")[0]
        _real_dirs = engine_mod._search_dirs
        _real_load = engine_mod._load_gpu_lib
        _real_dev = engine_mod._driver_devices
        engine_mod._search_dirs = lambda: [_d]
        try:
            _p = engine_mod.probe_gpu()
            check("GPU 探测：没有 CUDA 版产物 ⇒ 不可用",
                  _p["available"] is False and "产物" in _p["reason"],
                  json.dumps(_p, ensure_ascii=False))

            # 写几个字节的假产物：**真加载会失败**（不是合法 PE）—— 这一条不猴补，
            # 走的是真路径，正好验"缺 VC 运行库 / 产物损坏"那个诊断。
            (_d / _gpu_name).write_bytes(b"not a real dll")
            _p = engine_mod.probe_gpu()
            check("GPU 探测：产物在但加载不起来 ⇒ 不可用，原因是加载失败",
                  _p["available"] is False and "加载失败" in _p["reason"],
                  json.dumps(_p, ensure_ascii=False))

            engine_mod._load_gpu_lib = lambda p: (True, "")
            engine_mod._driver_devices = lambda: (0, "", "假的：没有驱动")
            check("GPU 探测：能加载但驱动报不出设备 ⇒ 不可用，且原因一路带出来",
                  engine_mod.probe_gpu()["available"] is False
                  and engine_mod.probe_gpu()["reason"] == "假的：没有驱动")

            engine_mod._driver_devices = lambda: (1, "6.1", "")
            _p = engine_mod.probe_gpu()
            check("GPU 探测：算力低于 CUDA 13 的地板（7.5）⇒ 不可用",
                  _p["available"] is False and "6.1" in _p["reason"],
                  json.dumps(_p, ensure_ascii=False))

            engine_mod._driver_devices = lambda: (2, "12.0", "")
            _p = engine_mod.probe_gpu()
            check("GPU 探测：三道门全过 ⇒ 可用，并报出设备数与算力",
                  _p["available"] is True and _p["devices"] == 2
                  and _p["compute_cap"] == "12.0" and _p["lib"] == _gpu_name,
                  json.dumps(_p, ensure_ascii=False))
        finally:
            engine_mod._search_dirs = _real_dirs
            engine_mod._load_gpu_lib = _real_load
            engine_mod._driver_devices = _real_dev

    # 探测结果**进进程内缓存**（用户拍板"游戏启动时检查一次就好"）——
    # 每开一次设置抽屉就 LoadLibrary 一遍纯属浪费。
    _calls = []
    _real_probe = engine_mod.probe_gpu
    engine_mod.probe_gpu = lambda: (
        _calls.append(1)
        or {"available": True, "devices": 1, "lib": "x", "compute_cap": "12.0",
            "reason": ""}
    )
    try:
        engine_mod.gpu_probe(refresh=True)
        engine_mod.gpu_probe()
        engine_mod.gpu_probe()
        check("GPU 探测：结果进进程内缓存，只探一次", len(_calls) == 1, str(len(_calls)))
        engine_mod.gpu_probe(refresh=True)
        check("GPU 探测：refresh=True 能强制重探（诊断用）", len(_calls) == 2, str(len(_calls)))
    finally:
        engine_mod.probe_gpu = _real_probe
        engine_mod.gpu_probe(refresh=True)

    # 4. 工具调用形状（Agent 层按 OpenAI 形状消费，见 agents/base.py）
    calls = _to_openai_calls([
        {"name": "read_scene", "arguments": {"scene": "大厅"}},
        {"name": "bad", "arguments": None},   # 参数没解析出来 → 丢掉
        {"name": "text", "arguments": "{}"},  # 不是对象 → 丢掉
    ])
    check("引擎：tool_calls 转成 OpenAI 形状",
          len(calls) == 1 and calls[0]["type"] == "function"
          and bool(calls[0]["id"])
          and calls[0]["function"]["name"] == "read_scene"
          and json.loads(calls[0]["function"]["arguments"]) == {"scene": "大厅"},
          json.dumps(calls, ensure_ascii=False))
    check("引擎：Provider 声明支持工具调用（Qwen3 原生 tool calling）",
          EngineProvider.supports_tools is True)

    # 4b. 生成锁：引擎在**同一份权重**上不支持并发 generate（两个房间同时跑 →
    #     服务器进程无声消失：日志停在「已创建会话 #2」，无异常栈）。所以锁必须按
    #     **权重路径**共享 —— 一个房间一个 Provider，实例级锁在多房间时形同虚设。
    check("引擎：同一份权重的生成锁是同一把（全局串行）",
          engine_mod.model_lock("D:/m/Qwen3-1.7B.gguf") is engine_mod.model_lock("D:/m/Qwen3-1.7B.gguf"))
    check("引擎：不同权重的生成锁互不影响（可并行）",
          engine_mod.model_lock("D:/m/A.gguf") is not engine_mod.model_lock("D:/m/B.gguf"))
    _pa = EngineProvider(None, "D:/m/Qwen3-1.7B.gguf")
    _pb = EngineProvider(None, "D:/m/Qwen3-1.7B.gguf")
    check("引擎：同权重的两个 Provider 共用生成锁（多房间并发被真正串行化）",
          _pa._gen_lock is _pb._gen_lock is engine_mod.model_lock("D:/m/Qwen3-1.7B.gguf"))

    # 4c. model_for 的加载在锁内：避免两个线程同时 miss、重复加载同一份 GGUF。
    class _CountingLib(engine_mod.EngineLib):
        def __init__(self):  # 不碰真实 dll
            self._lock = __import__("threading").Lock()
            self._models = {}
            self.loads = 0

        def load_model(self, model_path, **kw):
            self.loads += 1
            return 100 + self.loads

    _lib = _CountingLib()
    _h1 = _lib.model_for("D:/m/X.gguf")
    _h2 = _lib.model_for("D:/m/X.gguf")
    check("引擎：同一路径只加载一次权重", _h1 == _h2 and _lib.loads == 1, f"loads={_lib.loads}")

    # 4d. needs_warmup：建会话之前为真，跑过一次之后为假（决定要不要在等待室预热）
    class _WarmLib:
        path = "D:/fake/rpg_engine.dll"

        def model_for(self, *a, **k):
            return 11

        def new_session(self, *a, **k):
            return 22

        def destroy_session(self, s):
            pass

        def generate(self, sess, req):
            return {"text": "好", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        def model_backend(self, m):
            return "gpu"

    p_w = EngineProvider(_WarmLib(), "D:/m/W.gguf")
    check("引擎：还没生成过时 needs_warmup 为真", p_w.needs_warmup is True, "")
    p_w._ensure()
    check("引擎：建了会话之后 needs_warmup 为假", p_w.needs_warmup is False, "")

    # 5. 随机种子：引擎是纯函数，随机数由宿主给的 seed 展开。
    #    「谁带 seed、谁带 temperature」是一条协议（种子落在会话上、温度落在请求上），
    #    用假 lib 就能验，不需要 1.8 GB 权重。
    check("引擎：derive_seed 跨进程稳定（换成内置 hash 就会挂）",
          engine_mod.derive_seed(123456789, "dm") == 16183909127468317860,
          str(engine_mod.derive_seed(123456789, "dm")))
    check("引擎：derive_seed 落在 uint64 内",
          0 <= engine_mod.derive_seed(0, "dm") <= engine_mod.MAX_SEED
          and 0 <= engine_mod.derive_seed(engine_mod.MAX_SEED, "x") <= engine_mod.MAX_SEED)
    check("引擎：不同会话拿到不同种子",
          len({engine_mod.derive_seed(7654321, lb)
               for lb in ("dm", "assistant", "advisor:阿甲")}) == 3)
    check("引擎：不同房间拿到不同种子",
          engine_mod.derive_seed(1, "dm") != engine_mod.derive_seed(2, "dm"))

    class _FakeLib:
        """只验协议形状，不碰真实 dll。"""

        def __init__(self):
            self.session_cfg = None
            self.request = None

        def model_for(self, path, **kw):
            return 1

        def new_session(self, model, **kw):
            self.session_cfg = kw
            return 2

        def generate(self, session, req):
            self.request = req
            return {"text": "ok", "usage": {}}

        def destroy_session(self, session):
            pass

    fake = _FakeLib()
    EngineProvider(fake, "Z:/fake.gguf", context=1024, max_tokens=64,
                   temperature=0.9, seed=4242
                   )._run([{"role": "user", "content": "在吗"}], None)
    check("引擎：种子在建会话时一次性交给引擎（整局固定）",
          fake.session_cfg.get("seed") == 4242, str(fake.session_cfg))
    check("引擎：温度每轮随请求传（设置里改完即时生效）",
          fake.request.get("temperature") == 0.9 and "temperature" not in fake.session_cfg,
          str(fake.request))

    # 后端：Provider 把"请求的"与"实际的"都记下来（假 lib 没有 path / model_backend，
    # 那条日志必须**静默跳过**而不是把一次生成搞挂 —— 这正是下面这条在守的）。
    _pd = EngineProvider(fake, "Z:/fake.gguf", backend="gpu")
    check("引擎：Provider 描述带上请求的后端",
          _pd.describe()["backend"] == "gpu" and _pd.describe()["lib_backend"] == "",
          json.dumps(_pd.describe(), ensure_ascii=False))
    check("引擎：没有 path / model_backend 的库不会把后端日志搞崩",
          _pd._ensure() == 2 and _pd.actual_backend == "")

    fake2 = _FakeLib()
    EngineProvider(fake2, "Z:/f.gguf")._ensure()
    check("引擎：没给种子时把 None 原样带下去",
          fake2.session_cfg.get("seed") is None, str(fake2.session_cfg))

    # 「不带种子」的真正落点在 EngineLib.new_session：不往会话配置里写这个键
    # （引擎侧"没写 = 用它自己的默认"，于是 M4 的老请求依旧逐位一致）。
    from server.engine import EngineLib

    seen = {}

    class _StubCore:
        def rpg_engine_session_create(self, model, buf):
            seen["cfg"] = json.loads(buf.decode("utf-8"))
            return 7

    stub = object.__new__(EngineLib)
    stub._lib = _StubCore()
    stub.new_session(1, max_seq=512, max_tokens=32)
    check("引擎：不给种子时会话配置里没有 seed 键", "seed" not in seen["cfg"], str(seen["cfg"]))
    stub.new_session(1, seed=4242)
    check("引擎：给了种子就写进会话配置", seen["cfg"].get("seed") == 4242, str(seen["cfg"]))
    stub.new_session(1, seed=(1 << 70) + 5)
    check("引擎：越界的种子被钳进 uint64",
          seen["cfg"]["seed"] == ((1 << 70) + 5) & engine_mod.MAX_SEED, str(seen["cfg"]))

    leaked = describe_agents({}, {"kind": "engine", "model": "Q.gguf", "seed": 4242})["advisor"]
    check("引擎：公开状态不含种子（只进服务端日志）",
          "seed" not in json.dumps(leaked, ensure_ascii=False), json.dumps(leaked, ensure_ascii=False))


# ==================== 大厅 / 房间 ====================
# 为什么单开一节：大厅是**协议之外**唯一会改房间生命周期的东西（建房 / 开局 /
# 踢人 / 转让 / 解散），而它每一步都动 `rooms` 这个全局字典 —— 用假连接在进程内
# 走一遍，比在真服务器上二分快得多，也才能把「最后一人走了要不要销毁」这种
# 边界钉死。


class _FakeWS:
    """假连接：只记账。`Room` 只用到 `send_text` / `close` 两个方法。"""

    def __init__(self, label=""):
        self.label = label
        self.sent = []
        self.closed = False

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        self.closed = True

    def types(self):
        return [m.get("type") for m in self.sent]

    def last(self):
        return self.sent[-1] if self.sent else {}

    def texts(self):
        return " ".join(str(m.get("text") or "") for m in self.sent)

    def clear(self):
        self.sent = []


async def run_unit_lobby():
    print("== 大厅 / 房间（进程内） ==")
    from server import main as M
    from server.lobby import (
        DEFAULT_MAX_PLAYERS,
        MAX_NOTE,
        MAX_ROOM_NAME,
        VIS_PRIVATE,
        VIS_PUBLIC,
        build_public,
        clean_create,
        clean_settings,
        password_ok,
    )
    from server.script_loader import (
        DEFAULT_PLAYERS,
        MAX_PLAYERS_LIMIT,
        normalize_players,
        parse_script,
    )

    # ---- 1. 剧本声明的人数：`players` 是**可选**段 ----
    # 「没写」和「写坏」要区别对待：没写是正常情况（不能告警），写坏才告警 + 夹住。
    cases = [
        (None, dict(DEFAULT_PLAYERS), 0, "没写这个段 = 用缺省且不告警"),
        ("", dict(DEFAULT_PLAYERS), 0, "空字符串按没写处理"),
        (6, {"min": 1, "max": 6, "recommended": 6}, 1, "简写整数 = 只声明上限"),
        ("3", {"min": 1, "max": 3, "recommended": 3}, 1, "简写数字串也认"),
        ({"min": 2, "max": 5, "recommended": 4}, {"min": 2, "max": 5, "recommended": 4}, 0, "完整写法"),
        ({"min": 5, "max": 2}, {"min": 2, "max": 5, "recommended": 5}, 1, "区间写反了对调"),
        ({"min": 2, "max": 99}, {"min": 2, "max": MAX_PLAYERS_LIMIT, "recommended": MAX_PLAYERS_LIMIT}, 1, "超硬上限夹住"),
        ({"min": 2, "max": 4, "recommended": 9}, {"min": 2, "max": 4, "recommended": 4}, 1, "recommended 出区间夹回来"),
        ([1, 2], dict(DEFAULT_PLAYERS), 1, "类型不对告警并退回缺省"),
        ("abc", dict(DEFAULT_PLAYERS), 1, "认不出来告警并退回缺省"),
        (True, dict(DEFAULT_PLAYERS), 1, "布尔不是数字"),
    ]
    for raw, want, want_warn, label in cases:
        got, warns = normalize_players(raw)
        check(
            f"剧本 players：{label}",
            got == want and len(warns) == want_warn,
            f"got={got} warns={warns}",
        )

    doc = parse_script(SCRIPT.read_text(encoding="utf-8"), source=str(SCRIPT), name="古堡秘宝")
    check(
        "自带剧本 sample_script 声明了 players",
        doc.players.get("min") == 2 and doc.players.get("max") == 4,
        str(doc.players),
    )
    tdoc = parse_script(TOTSK.read_text(encoding="utf-8"), source=str(TOTSK), name="蛇王墓")
    check("自带剧本 totsk_l1 声明了 players", tdoc.players.get("max") == 5, str(tdoc.players))
    check("players 进了 to_dict（接口/前端拿得到）", doc.players == doc.to_dict()["players"],
          str(doc.to_dict().get("players")))

    # ---- 2. lobby 的纯逻辑（建房清洗 / 改设置补丁 / 密码校验 / 对外投影）----
    sp = {"min": 2, "max": 4, "recommended": 4}

    params, err = clean_create({}, sp)
    check("建房：空参数用剧本的 recommended", params["max_players"] == 4 and err is None, str(params))
    check("建房：下限取剧本的 min", params["min_players"] == 2, str(params["min_players"]))
    check("建房：缺省是公开房", params["visibility"] == VIS_PUBLIC, str(params["visibility"]))

    params, _ = clean_create(
        {"name": "  大厅  冒烟  ", "max_players": "3", "visibility": "private", "password": "p" * 99}, sp
    )
    check("建房：名字折叠内部空白", params["name"] == "大厅 冒烟", params["name"])
    check("建房：数字串上限被认成 int", params["max_players"] == 3, str(params["max_players"]))
    check("建房：私密房被认下来", params["visibility"] == VIS_PRIVATE, str(params["visibility"]))
    check("建房：密码截到 32 字符", len(params["password"]) == 32, str(len(params["password"])))

    params, _ = clean_create(
        {"name": "x" * 99, "note": "y" * 200, "visibility": "secret", "max_players": True,
         "script": 123}, sp
    )
    check("建房：房名截到 24 字符", len(params["name"]) == MAX_ROOM_NAME, str(len(params["name"])))
    check("建房：备注截到 80 字符", len(params["note"]) == MAX_NOTE, str(len(params["note"])))
    check("建房：乱写的可见性退回公开", params["visibility"] == VIS_PUBLIC, str(params["visibility"]))
    check("建房：上限给了布尔 = 给不出数字，用默认", params["max_players"] == 4, str(params["max_players"]))
    check("建房：非字符串剧本名被丢掉", params["script"] == "", repr(params["script"]))

    check("建房：上限被夹到剧本上限", clean_create({"max_players": 99}, sp)[0]["max_players"] == 4, "")
    check("建房：低于剧本下限被抬到下限", clean_create({"max_players": 1}, sp)[0]["max_players"] == 2, "")
    params, _ = clean_create(None)
    check(
        "建房：payload 整个为 None 也给出一套完整值",
        params["max_players"] == DEFAULT_MAX_PLAYERS and params["min_players"] >= 1,
        str(params),
    )

    cur = {"min_players": 2}
    check("改设置：只带上提到的字段", clean_settings({"note": "hi"}, cur, sp) == {"note": "hi"}, "")
    check(
        "改设置：改备注**不会**顺手清掉密码",
        "password" not in clean_settings({"note": "hi"}, cur, sp),
        str(clean_settings({"note": "hi"}, cur, sp)),
    )
    check("改设置：空密码 = 显式清空", clean_settings({"password": ""}, cur, sp).get("password") == "", "")
    check("改设置：clear_password 也清空", clean_settings({"clear_password": True}, cur, sp).get("password") == "", "")
    patch = clean_settings({"visibility": "nope", "max_players": 99}, cur, sp)
    check("改设置：非法可见性忽略，上限仍被夹", "visibility" not in patch and patch.get("max_players") == 4, str(patch))
    check("改设置：非字典补丁不炸", clean_settings("不是字典", cur, sp) == {}, "")
    check("改设置：空补丁 = 空 patch", clean_settings({}, cur, sp) == {}, "")

    check("密码：没设密码恒通过（不填、乱填都行）", password_ok("", None) and password_ok("", "随便"), "")
    check("密码：精确匹配才放行（大小写敏感）", password_ok("pw", "pw") and not password_ok("pw", "PW"), "")
    check("密码：没填 / 非字符串一律不放行", not password_ok("pw", None) and not password_ok("pw", 123), "")

    pub = build_public(
        code="A1", name="", note="", script_title="古堡秘宝", owner="甲", online=2,
        max_players=4, phase="waiting", visibility=VIS_PRIVATE, has_password=True, created_at=0,
    )
    check("列表项：房名缺省用剧本名", pub["name"] == "古堡秘宝", pub["name"])
    check("列表项：只有 has_password，没有密码本体",
          pub["has_password"] is True and "password" not in pub, str(sorted(pub)))
    check("列表项：带中文阶段标签（界面直接用）", pub["phase_label"] == "等待中", pub["phase_label"])

    # ---- 3. 假 WebSocket 驱动**真实**的 Room ----
    saved_rooms = dict(M.rooms)
    saved_chronicle = M.CHRONICLE_ENABLED
    M.rooms.clear()
    # 剧情档案会往 data/ 里写 jsonl。进程内用例不需要它，关掉 —— 免得每次跑测试
    # 都在用户的数据目录里堆一份 chronicle-*.jsonl。
    M.CHRONICLE_ENABLED = False
    try:
        room = M.Room(
            "ABC12", script_store=ScriptStore(SCRIPT),
            create={"name": "测试房", "note": "备注", "max_players": 2,
                    "visibility": VIS_PUBLIC, "password": "pw"},
        )
        M.rooms["ABC12"] = room
        a, b = _FakeWS("甲"), _FakeWS("乙")

        info = room.lobby_info()
        check("房间：房名来自建房参数", info["name"] == "测试房", info["name"])
        check("房间：备注落到房间", info["note"] == "备注")
        check("房间：容量来自建房参数（可覆盖剧本）", info["max_players"] == 2, str(info["max_players"]))
        check("房间：初始是等待阶段", info["phase"] == "waiting", info["phase"])
        check("房间：快照里没有密码本体",
              info["has_password"] is True and "password" not in info, str(sorted(info)))

        check("房间：密码错被拒", (room.join_denied("甲", "bad") or {}).get("code") == "password")
        check("房间：密码对放行", room.join_denied("甲", "pw") is None)

        await room.on_join("甲", {}, a)
        check("房间：首位加入者成为房主", room.owner == "甲", str(room.owner))
        check("房间：连入即拿到房间快照", "room" in a.types(), str(a.types()))
        await room.on_join("乙", {}, b)
        check("房间：在线人数 = 2", room.lobby_info()["online"] == 2, str(room.lobby_info()["online"]))
        check("房间：满员后新面孔被拒", (room.join_denied("丙", "pw") or {}).get("code") == "full")
        # 容量只拦新面孔：否则满员的房间会把掉线重连的人永久关在外面
        check("房间：满员时老面孔（断线重连/换标签页）照旧放行", room.join_denied("甲", "pw") is None)
        check("房间：满员时先问密码、后问容量（不给容量当密码旁路）",
              (room.join_denied("丙", "乱填") or {}).get("code") == "password", "")

        # ---- 等待阶段的行动门控：既没开局也没剧情，放行只会让 DM 去猜 ----
        a.clear()
        check("等待阶段：action 不放行", await room.require_playing(a) is False)
        check("等待阶段：回的是 not_started", a.last().get("type") == "not_started", str(a.last()))

        b.clear()
        await room.on_start("乙", b)
        check("开始：非房主被拒", b.last().get("type") == "error", str(b.last()))
        check("开始：阶段没被改动", room.phase == "waiting", room.phase)

        a.clear()
        await room.on_start("甲", a)
        check("开始：房主点完进入进行中", room.phase == "playing", room.phase)
        check(
            "开始：广播了房间快照且阶段已翻转",
            any(m.get("type") == "room" and m.get("room", {}).get("phase") == "playing" for m in a.sent),
            str(a.types()),
        )
        check("开始：广播了开局提示", "开始了这一局" in a.texts(), a.texts()[:80])
        check("开始：广播了开场旁白（剧本的 public_text）", "narration" in a.types(), str(a.types()))
        check("开始：广播了状态快照", "state" in a.types(), str(a.types()))
        check("开始后：action 过阶段门", await room.require_playing(a) is True)
        a.clear()
        await room.on_start("甲", a)
        check("开始：已经开局了再点要明确回绝",
              a.last().get("type") == "error" and "已经开始" in a.last().get("message", ""), str(a.last()))

        # ---- 房主改设置 ----
        b.clear()
        await room.on_room_settings("乙", {"note": "越权"}, b)
        check("改设置：非房主被拒", b.last().get("type") == "error", str(b.last()))
        check("改设置：备注没被动", room.note == "备注", room.note)

        a.clear()
        await room.on_room_settings("甲", {"note": "新备注", "max_players": 99}, a)
        check("改设置：房主能改备注", room.note == "新备注", room.note)
        check("改设置：上限被夹进剧本区间", room.max_players == 4, str(room.max_players))
        check("改设置：广播了变更项", "房间设置" in a.texts(), a.texts()[:80])
        check(
            "改设置：快照跟着更新（列表/等待室不用自己算）",
            any(m.get("type") == "room" and m.get("room", {}).get("note") == "新备注" for m in a.sent),
            str(a.types()),
        )
        a.clear()
        await room.on_room_settings("甲", {"note": "新备注"}, a)
        check("改设置：原样提交要明确回一句「没有变化」",
              a.last().get("type") == "error" and "没有变化" in a.last().get("message", ""), str(a.last()))

        # ---- 转让：DM 的次级 prompt 只发房主，所以换人必须按人重发 ----
        b.clear()
        await room.on_transfer("甲", "乙", a)
        check("转让：房主换人", room.owner == "乙", str(room.owner))
        sync = [m for m in b.sent if m.get("type") == "room_sync"]
        check("转让：新得的人收到 room_sync", bool(sync), str(b.types()))
        check("转让：新房主拿到 DM 次级 prompt", bool(sync and (sync[0].get("prompt") or {}).get("dm")), "")
        check(
            "转让：快照里的房主也更新了",
            any(m.get("type") == "room" and m.get("room", {}).get("owner") == "乙" for m in b.sent),
            str(b.types()),
        )
        b.clear()
        await room.on_transfer("乙", "乙", b)
        check("转让：转给自己要被回绝", b.last().get("type") == "error", str(b.last()))
        b.clear()
        await room.on_transfer("乙", "查无此人", b)
        check("转让：转给不在房里的人要被回绝", b.last().get("type") == "error", str(b.last()))
        b.clear()
        await room.on_transfer("甲", "乙", b)
        check("转让：已经不是房主了就不能再转", b.last().get("type") == "error", str(b.last()))

        # ---- 踢人：房主不能踢自己、踢不掉的人要说清楚、被踢的要留痕 ----
        a.clear()
        await room.on_kick("甲", "乙", a)
        check("踢人：非房主被拒", a.last().get("type") == "error", str(a.last()))
        check("踢人：没人被踢掉", len(room.clients) == 2, str(len(room.clients)))
        b.clear()
        await room.on_kick("乙", "乙", b)
        check("踢人：房主不能请自己出去（提示去解散）",
              b.last().get("type") == "error" and "解散" in b.last().get("message", ""), str(b.last()))
        b.clear()
        await room.on_kick("乙", "查无此人", b)
        check("踢人：不在房里的人要被回绝", b.last().get("type") == "error", str(b.last()))

        b.clear()
        await room.on_kick("乙", "甲", b)
        check("踢人：房主能请人走", [n for n, _ in room.clients] == ["乙"], str([n for n, _ in room.clients]))
        check("踢人：被踢方收到 kicked", a.sent and a.sent[-1].get("type") == "kicked", str(a.sent[-1:]))
        check("踢人：服务端自己把连接也断了（不靠客户端自觉）", a.closed is True)
        check("踢人：被踢的人被登记（断开时不再广播「离开队伍」）", "甲" in room.ejected)
        check("踢人：房间还在（人没走光就不销毁）", "ABC12" in M.rooms)

        # ---- 解散 ----
        room2 = M.Room("XYZ99", script_store=ScriptStore(SCRIPT), create={"name": "解散测试"})
        M.rooms["XYZ99"] = room2
        z = _FakeWS("丙")
        await room2.on_join("丙", {}, z)
        z.clear()
        await room2.on_close_room("丙", z)
        check("解散：房间被标记关闭", room2.closed is True)
        check("解散：收到 room_closed", "room_closed" in z.types(), str(z.types()))
        check("解散：连接被服务端断开", z.closed is True)
        check("解散：已从大厅删掉", "XYZ99" not in M.rooms, str(list(M.rooms)))
        check("解散：房主名单也清空（收尾时不会重复销毁）", z.sent and not room2.clients, str(room2.clients))

        # ---- 缺省建房（房号不存在时的隐式建房也走这条）----
        r3 = M.Room("DEF00")
        check("缺省建房：人数用剧本的 recommended",
              r3.max_players == M.store.players["recommended"], str(r3.max_players))
        check("缺省建房：公开", r3.visibility == VIS_PUBLIC, r3.visibility)
        check("缺省建房：房名缺省用剧本名", r3.name == M.store.title, r3.name)
        M.rooms["DEF00"] = r3

        # ---- 大厅列表：私密房不上榜 ----
        M.rooms["PRV01"] = M.Room(
            "PRV01", script_store=ScriptStore(SCRIPT),
            create={"name": "私密房", "visibility": VIS_PRIVATE, "password": "pw"},
        )
        listed = await M.api_rooms()
        codes = [r["code"] for r in listed["rooms"]]
        check("大厅列表：公开房在榜上", "ABC12" in codes, str(codes))
        check("大厅列表：私密房不在榜上（这是建私密房的全部意义）", "PRV01" not in codes, str(codes))
        check("大厅列表：等待中的排在已开局的前面",
              codes.index("DEF00") < codes.index("ABC12"), str(codes))
        check("大厅列表：任何一项都不含密码本体",
              all("password" not in r for r in listed["rooms"]), str(listed["rooms"][:1]))
        check("大厅列表：带建房表单的缺省值（按活动剧本算）",
              isinstance((listed.get("defaults") or {}).get("players"), dict), str(listed.get("defaults")))
        check("大厅列表：没有默认活动剧本", listed.get("active_title") in (None, ""),
              str(listed.get("active_title")))

        # ---- 进不去的人不该把房间搞脏 ----
        # join_denied 只是"能不能进"的判断，这里顺带确认它**没有副作用**：
        # 被拒的名字不该混进玩家表，也不该占容量。
        check("被拒的请求不改房间状态",
              "丙" not in room.state.players and room.lobby_info()["online"] == 1,
              f"{list(room.state.players)} online={room.lobby_info()['online']}")
    finally:
        M.CHRONICLE_ENABLED = saved_chronicle
        M.rooms.clear()
        M.rooms.update(saved_rooms)


async def _ws_closed(ws, timeout=5.0):
    """等服务端把这条连接关掉。True = 确实被关了。

    「服务端说了 kicked / join_denied / room_closed，但连接还开着」是个真出现过的
    bug（一个不听话的客户端会永远停在游戏屏上，而且它已经不在 clients 里、再也收不到
    任何广播），所以这里必须等**连接**真的断，而不是只等那条消息。
    """
    end = time.time() + timeout * _TIMEOUT_SCALE
    while time.time() < end:
        try:
            await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
        except asyncio.TimeoutError:
            return False
        except Exception:  # noqa: BLE001  ConnectionClosed* / 任何断开
            return True
    return False


async def run_live_lobby():
    print("== 实时大厅 / 房间校验（需服务器已启动） ==")
    try:
        import websockets
    except ImportError:
        print("  [SKIP] 未安装 websockets，跳过")
        return
    try:
        import httpx
    except ImportError:
        httpx = None

    base = http_base()
    url = ws_url()
    tag = str(int(time.time() * 1000) % 100000)
    room, pvt, wrk = "LOB" + tag, "PVT" + tag, "WRK" + tag
    opened = []

    async def dial():
        w = await websockets.connect(url, proxy=None)
        opened.append(w)
        return w

    async def join(w, name, code, create=None, password=None):
        msg = {"type": "join", "name": name, "room": code, "character": {"cls": "战士"}}
        if create is not None:
            msg["create"] = create
        if password is not None:
            msg["password"] = password
        await w.send(json.dumps(msg, ensure_ascii=False))

    async def rooms_of():
        if httpx is None:
            return None
        try:
            return httpx.get(base + "/api/rooms", timeout=8, trust_env=False).json()
        except Exception:  # noqa: BLE001  服务器没起来后面本来就会 SKIP
            return None

    try:
        try:
            a = await dial()
        except Exception as e:  # noqa: BLE001
            print(f"  [SKIP] 无法连接服务器 {url}：{e}")
            return

        # ---- 建房（带元数据）----
        await join(a, "房主甲", room,
                   create={"name": "大厅冒烟房", "note": "自动测试", "max_players": 2,
                           "visibility": "public"})
        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        ri = (wa or {}).get("room_info") or {}
        check("大厅：建房者拿到 welcome", bool(wa), str(wa)[:100])
        check("大厅：welcome 带回房名与备注",
              ri.get("name") == "大厅冒烟房" and ri.get("note") == "自动测试", str(ri)[:140])
        check("大厅：人数上限来自建房参数（房主可覆盖剧本声明）",
              ri.get("max_players") == 2, str(ri.get("max_players")))
        check("大厅：初始阶段是等待中", ri.get("phase") == "waiting", str(ri.get("phase")))
        check("大厅：房主是第一个进来的人", ri.get("owner") == "房主甲", str(ri.get("owner")))
        check("大厅：快照不下发密码本体", "password" not in ri, str(sorted(ri)))

        data = await rooms_of()
        if data is None:
            print("  [SKIP] 没有 httpx，跳过大厅列表校验")
        else:
            codes = [r["code"] for r in data.get("rooms", [])]
            check("大厅列表：公开房间在榜上", room in codes, str(codes[:6]))
            row = next((r for r in data["rooms"] if r["code"] == room), {})
            check("大厅列表：带房主与当前人数",
                  row.get("owner") == "房主甲" and row.get("online") == 1, str(row)[:160])
            check("大厅列表：不下发密码本体", "password" not in row, str(sorted(row)))
            check("大厅列表：缺省人数按当前活动剧本算",
                  isinstance((data.get("defaults") or {}).get("players"), dict), str(data.get("defaults")))

        # ---- 等待阶段的行动门控（进行中之后不再发真行动，见下面的说明）----
        await a.send(json.dumps({"type": "action", "text": "随便动一下"}, ensure_ascii=False))
        na = await recv_until(a, lambda m: m["type"] == "not_started")
        check("大厅：等待阶段行动被挡（not_started）", bool(na), str(na)[:120])
        await a.send(json.dumps({"type": "roll"}, ensure_ascii=False))
        nr = await recv_until(a, lambda m: m["type"] in ("not_started", "dice"))
        check("大厅：等待阶段掷骰也被挡", bool(nr) and nr["type"] == "not_started", str(nr)[:120])

        # ---- 第二个人进来（等待阶段 = 一起等人齐，不是「中途加入」）----
        b = await dial()
        await join(b, "玩家乙", room)
        wb = await recv_until(b, lambda m: m["type"] == "welcome")
        check("大厅：第二人在等待阶段进来（late=False）",
              bool(wb) and wb.get("late") is False, str(wb)[:120])
        hint = await recv_until(
            b, lambda m: m["type"] == "system" and "等待房主开始" in (m.get("text") or "")
        )
        check("大厅：等待室明说「在等人齐 + 开局前不能行动」（不是静默卡住）",
              bool(hint), str(hint)[:120])
        ra = await recv_until(a, lambda m: m["type"] == "room" and m["room"]["online"] == 2)
        check("大厅：人数一变就重播快照（房里的人不用手动刷新）", bool(ra), str(ra)[:140])

        # ---- 满员拒绝 ----
        c = await dial()
        await join(c, "玩家丙", room)
        deny = await recv_until(c, lambda m: m["type"] == "join_denied", timeout=4)
        check("大厅：满员被拒（code=full）", bool(deny) and deny.get("code") == "full", str(deny)[:140])
        check("大厅：被拒后服务端关掉连接", await _ws_closed(c), "")

        # ---- 开始（只有房主）----
        await b.send(json.dumps({"type": "start"}, ensure_ascii=False))
        eb = await recv_until(b, lambda m: m["type"] == "error")
        check("大厅：非房主不能开始",
              bool(eb) and "房主" in (eb.get("message") or ""), str(eb)[:140])

        await a.send(json.dumps({"type": "start"}, ensure_ascii=False))
        snap = await recv_until(b, lambda m: m["type"] == "room" and m["room"]["phase"] == "playing")
        check("大厅：房主开始后阶段翻转（所有人都收到）", bool(snap), str(snap)[:160])
        sy = await recv_until(b, lambda m: m["type"] == "system" and "开始了这一局" in (m.get("text") or ""))
        check("大厅：开局广播了提示", bool(sy), str(sy)[:140])
        # ⚠️ 这里**故意**不再发一条真 action 去验「开局后门控放行」：一次行动会让
        # DM 推理几十秒到几分钟并占住房间锁，后面的踢人/解散全得跟着等。放行这件事
        # 由进程内那节（require_playing == True）覆盖，这里只钉「阶段已翻转」。

        # ---- 转让 ----
        await a.send(json.dumps({"type": "transfer", "target": "玩家乙"}, ensure_ascii=False))
        s1 = await recv_until(b, lambda m: m["type"] == "room_sync")
        check("大厅：转让后新房主收到 room_sync", bool(s1), str(s1)[:100])
        t1 = await recv_until(b, lambda m: m["type"] == "room" and m["room"]["owner"] == "玩家乙")
        check("大厅：转让后快照里的房主换人", bool(t1), str(t1)[:160])

        # ---- 踢人：被请的人连接必须真的断（不靠客户端自觉）----
        await b.send(json.dumps({"type": "kick", "target": "房主甲"}, ensure_ascii=False))
        ka = await recv_until(a, lambda m: m["type"] == "kicked", timeout=4)
        check("大厅：房主能请人走", bool(ka) and ka.get("by") == "玩家乙", str(ka)[:140])
        check("大厅：被请的人连接被服务端断掉", await _ws_closed(a), "")
        kb = await recv_until(b, lambda m: m["type"] == "room" and m["room"]["online"] == 1)
        check("大厅：踢完人数变 1（房间没被踢散）", bool(kb), str(kb)[:140])

        # ---- 解散 ----
        await b.send(json.dumps({"type": "close_room"}, ensure_ascii=False))
        kc = await recv_until(b, lambda m: m["type"] == "room_closed", timeout=4)
        check("大厅：房主能解散房间", bool(kc), str(kc)[:140])
        data2 = await rooms_of()
        if data2 is not None:
            check("大厅：解散后从列表消失",
                  room not in [r["code"] for r in data2.get("rooms", [])],
                  str([r["code"] for r in data2.get("rooms", [])][:8]))

        # ---- 私密房 + 密码 ----
        p = await dial()
        await join(p, "私密房主", pvt,
                   create={"name": "私密冒烟房", "visibility": "private", "password": "s3cr3t",
                           "max_players": 2})
        wp = await recv_until(p, lambda m: m["type"] == "welcome")
        rp = (wp or {}).get("room_info") or {}
        check("私密房：只给 has_password，不给密码本体",
              rp.get("has_password") is True and "password" not in rp, str(sorted(rp)))
        check("私密房：可见性就是私密", rp.get("visibility") == "private", str(rp.get("visibility")))
        data3 = await rooms_of()
        if data3 is not None:
            check("私密房：不进公开列表",
                  pvt not in [r["code"] for r in data3.get("rooms", [])],
                  str([r["code"] for r in data3.get("rooms", [])][:8]))

        q = await dial()
        await join(q, "想混进去的人", pvt, password="猜的")
        dq = await recv_until(q, lambda m: m["type"] == "join_denied", timeout=4)
        check("私密房：密码错被拒（code=password）",
              bool(dq) and dq.get("code") == "password", str(dq)[:140])
        check("私密房：被拒的连接也被关掉", await _ws_closed(q), "")

        r = await dial()
        await join(r, "知道密码的人", pvt, password="s3cr3t")
        wr = await recv_until(r, lambda m: m["type"] == "welcome")
        check("私密房：密码对就能进", bool(wr), str(wr)[:120])

        await p.send(json.dumps({"type": "close_room"}, ensure_ascii=False))
        cr = await recv_until(r, lambda m: m["type"] == "room_closed", timeout=4)
        check("私密房：解散通知也发给非房主", bool(cr), str(cr)[:140])
        check("私密房：非房主的连接被服务端断掉", await _ws_closed(r), "")

        # ---- 房主掉线：房间按「有人连线就活着」留下，房主身份自动移交 ----
        x = await dial()
        await join(x, "会掉线的房主", wrk, create={"name": "掉线测试房", "max_players": 3})
        y = await dial()
        await join(y, "接班的人", wrk)
        wx = await recv_until(x, lambda m: m["type"] == "welcome")
        wy = await recv_until(y, lambda m: m["type"] == "welcome")
        check("掉线：两个人都在房里", bool(wx) and bool(wy), f"{bool(wx)}/{bool(wy)}")
        # 直接关连接 = 模拟掉线（不发 leave），走的是服务端收尾那条路
        await x.close()
        # ⚠️ 顺序必须按服务端**实际发出的次序**找：`recv_until` 会把找匹配之前读到的
        # 消息丢掉，所以先找 room_sync 再找 room 快照 —— 反了就永远找不到前者
        # （收尾那条路是：离开公告 → 移交公告 → room_sync + state → room 快照）。
        tx = await recv_until(y, lambda m: m["type"] == "system" and "房主已自动移交" in (m.get("text") or ""),
                              timeout=8)
        check("掉线：房主掉线后自动移交给在场最早的玩家", bool(tx), str(tx)[:140])
        ky = await recv_until(y, lambda m: m["type"] == "room_sync", timeout=6)
        check("掉线：接手的人拿到 room_sync（DM 次级 prompt 跟着走）", bool(ky), str(ky)[:100])
        hy = await recv_until(y, lambda m: m["type"] == "room" and m["room"]["owner"] == "接班的人")
        check("掉线：快照里的房主跟着换", bool(hy), str(hy)[:160])
    finally:
        for w in opened:
            try:
                await w.close()
            except Exception:  # noqa: BLE001
                pass


async def run_unit_card():
    print("== 进程内校验：角色卡（分析结果规范化 / 点数分配 / 检定加值 / 缓存） ==")
    import tempfile

    from server import character_card as cc

    # ---- normalize_card：模型吐出来的脏 JSON 必须洗成一份能用的卡 ----
    check("角色卡：None / 空对象不算合格", cc.normalize_card(None) == {} and cc.normalize_card({}) == {})
    check("角色卡：不是对象一律不合格", cc.normalize_card([1, 2]) == {})
    no_card = cc.normalize_card({"required": False, "reason": "纯叙事"})
    check(
        "角色卡：required=false 是合法结论（不是「没分析出来」）",
        no_card.get("required") is False and no_card.get("attributes") == [] and no_card.get("reason") == "纯叙事",
    )
    check("角色卡：说需要却不给维度 = 没产出", cc.normalize_card({"required": True}) == {})

    card = cc.normalize_card(
        {
            "required": True, "reason": "有战斗与学识检定", "total_points": 6,
            "attributes": [
                {"key": "力量", "desc": "搬、劈、扛", "min": 8, "base": 10, "max": 16},
                {"key": "学识", "desc": "识破机关", "min": 10, "base": 10, "max": 18},
            ],
            "skill_map": {"战斗": "力量", "调查": "学识", "胡说": "不存在"},
        }
    )
    check("角色卡：合法输入原样通过", card.get("required") is True and len(card["attributes"]) == 2)
    check(
        "角色卡：skill_map 指向不存在的属性会被丢掉",
        "胡说" not in card["skill_map"] and card["skill_map"].get("调查") == "学识",
    )

    many = cc.normalize_card({"required": True, "attributes": [{"key": f"属性{i}"} for i in range(12)]})
    check("角色卡：维度最多 8 项", len(many["attributes"]) == 8)
    dup = cc.normalize_card({"required": True, "attributes": [{"key": "力量"}, {"key": "力量"}]})
    check("角色卡：重复的属性名只留一个", len(dup["attributes"]) == 1)
    longkey = cc.normalize_card({"required": True, "attributes": [{"key": "很长很长很长很长的属性名字"}]})
    check("角色卡：属性名截到 12 字", len(longkey["attributes"][0]["key"]) <= cc.MAX_KEY_LEN)
    bad_range = cc.normalize_card({"required": True, "attributes": [{"key": "力量", "min": 16, "max": 8}]})
    one = bad_range["attributes"][0]
    check("角色卡：区间反了按缺省重来", one["min"] < one["max"] and one["min"] == 8 and one["max"] == 16)
    over_base = cc.normalize_card({"required": True, "attributes": [{"key": "力量", "min": 8, "base": 99, "max": 16}]})
    check("角色卡：基础值越界被夹进区间", over_base["attributes"][0]["base"] == 16)
    huge = cc.normalize_card({"required": True, "total_points": 999, "attributes": [{"key": "力量"}]})
    check("角色卡：点数总量有硬上限", huge["total_points"] == cc.MAX_TOTAL_POINTS)
    okstr = cc.normalize_card({"required": True, "total_points": "6", "attributes": [{"key": "力量"}]})
    check("角色卡：点数是字符串也认", okstr["total_points"] == 6)
    junk = cc.normalize_card({"required": True, "attributes": [{"key": "力量"}, "我不对象", 42]})
    check("角色卡：非对象的属性项被跳过", len(junk["attributes"]) == 1)
    listmap = cc.normalize_card(
        {"required": True, "attributes": [{"key": "力量"}],
         "skill_map": [{"skill": "战斗", "attr": "力量"}, {"skill": "游泳", "attr": "不存在"}]}
    )
    check("角色卡：skill_map 写成列表也收（对象形态会被重复键悄悄折叠）",
          listmap["skill_map"] == {"战斗": "力量"}, str(listmap["skill_map"]))
    same = cc.normalize_card({"required": True, "attributes": [{"key": "侦查"}]})
    check("角色卡：技能名与属性同名时自动对上（skill_map 漏了也不至于完全不加值）",
          cc.attr_for_skill(same, "侦查") == "侦查")

    # ---- JSON 容错：小模型的手滑不该让整张卡丢掉 ----
    from server.agents.tool_agent import parse_json

    check("JSON 容错：结尾多一个 } 也能解析", parse_json('{"a":1}}') == {"a": 1}, str(parse_json('{"a":1}}')))
    check("JSON 容错：尾随逗号", parse_json('{"a":1,}') == {"a": 1})
    check("JSON 容错：``` 包裹照旧", parse_json('```json\n{"a":1}\n```') == {"a": 1})
    check("JSON 容错：实在解析不出来就是空对象", parse_json("我觉得这个本很棒") == {})

    # ---- 检定加值：10 为基准，每 2 点 ±1 ----
    check(
        "角色卡：修正公式（8→-1 / 9→-1 / 10→0 / 11→0 / 16→+3）",
        (cc.modifier(8), cc.modifier(9), cc.modifier(10), cc.modifier(11), cc.modifier(16)) == (-1, -1, 0, 0, 3),
        f"{[cc.modifier(v) for v in (8, 9, 10, 11, 16)]}",
    )
    check("角色卡：技能按名字精确命中", cc.attr_for_skill(card, "战斗") == "力量")
    check("角色卡：技能名多几个字也能命中（「战斗检定」→ 战斗）", cc.attr_for_skill(card, "战斗检定") == "力量")
    check("角色卡：没映射的技能不加值", cc.attr_for_skill(card, "游泳") is None)
    check("角色卡：空技能名不加值", cc.attr_for_skill(card, "") is None)
    check("角色卡：不需要卡的剧本对任何技能都不加值", cc.attr_for_skill(no_card, "战斗") is None)
    check("角色卡：按人算加值", cc.modifier_for(card, {"attrs": {"力量": 14}}, "战斗") == (2, "力量"))
    check("角色卡：没填卡的人加值 0（不会把难度带偏）", cc.modifier_for(card, {"cls": "战士"}, "战斗") == (0, ""))
    check("角色卡：属性值不是数字也当没填", cc.modifier_for(card, {"attrs": {"力量": "很高"}}, "战斗") == (0, ""))

    # ---- 点数分配校验（服务端权威） ----
    check(
        "角色卡：合法分配通过",
        cc.validate_allocation(card, {"力量": 14, "学识": 12}) == ({"力量": 14, "学识": 12}, []),
    )
    ok_default, errs = cc.validate_allocation(card, {})
    check("角色卡：什么都不填 = 全部落在基础值", ok_default == {"力量": 10, "学识": 10} and not errs, str(errs))
    check("角色卡：低于下限被拒", "不能低于" in cc.validate_allocation(card, {"力量": 4, "学识": 10})[1][0])
    check("角色卡：高于上限被拒", "不能高于" in cc.validate_allocation(card, {"力量": 20, "学识": 10})[1][0])
    check("角色卡：点数超了被拒", "点数超了" in cc.validate_allocation(card, {"力量": 16, "学识": 18})[1][0])
    check("角色卡：不是数字被拒", bool(cc.validate_allocation(card, {"力量": "很强", "学识": 10})[1]))
    check("角色卡：整个不是对象被拒", cc.validate_allocation(card, "x")[1] == ["角色卡格式不对"])
    check("角色卡：不需要卡就别提交", cc.validate_allocation(no_card, {})[1] == ["这个剧本不需要角色卡"])
    check("角色卡：已用点数只算基础值之上", cc.spent(card, {"力量": 14, "学识": 12}) == 6)
    check("角色卡：剩余点数", cc.remaining(card, {"力量": 12, "学识": 10}) == 4)

    # ---- 提示词 / 界面用的一行摘要 ----
    check("角色卡：一行摘要", cc.summary(card, {"attrs": {"力量": 14, "学识": 12}}) == "力量 14 · 学识 12")
    check("角色卡：没填就没有摘要", cc.summary(card, {}) == "")
    check(
        "角色卡：提示词里的玩家行带职业与属性",
        cc.player_line("亚瑟", {"cls": "战士", "attrs": {"力量": 14}}, card) == "亚瑟(战士 · 力量 14)",
    )
    check("角色卡：没卡时玩家行只留职业", cc.player_line("亚瑟", {"cls": "战士"}, None) == "亚瑟(战士)")
    client = cc.to_client(card)
    check(
        "角色卡：下发前端的卡不含 skill_map（那是剧本内幕）",
        "skill_map" not in client and len(client.get("attributes") or []) == 2,
    )

    # ---- 前端加减号的下限必须是 base（2026-10-21 修的回归）----
    # 点数花满后点减号若能压到 base 以下，used 被 max(0, v-base) 钳住不回升，
    # 加号又因剩余 0 被禁用 —— 这 1 点就永久丢了。静态断言 app.js 的三处下限。
    app_js = (SCRIPT.parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    check("角色卡：前端减号下限是 base 不是 min（防点数永久丢失回归）",
          "minus.disabled = cardDraft[a.key] <= a.base" in app_js
          and "Math.max(a.base, Math.min(a.max, cur + delta))" in app_js
          and "minus.disabled = cardDraft[a.key] <= a.min" not in app_js)

    # ---- 缓存：同一份剧本内容只分析一次 ----
    store_now = ScriptStore(SCRIPT, label="sample_script.json")
    key_a = cc.card_key(store_now)
    store_again = ScriptStore(SCRIPT, label="换个文件名.json")
    check("角色卡：同一份剧本内容 → 同一个缓存键（换文件名也算同一份）", key_a == cc.card_key(store_again))
    totsk_store = ScriptStore(TOTSK, label="totsk_l1.json")
    check("角色卡：不同剧本 → 不同键", cc.card_key(totsk_store) != key_a)

    tmpdir = Path(tempfile.mkdtemp(prefix="rpgbar-card-"))
    real_dir = cc.CARD_DIR
    cc.CARD_DIR = tmpdir
    try:
        check("角色卡：没缓存时读到 None", cc.load_cached(key_a) is None)
        cc.save_cached(key_a, card, title=store_now.title)
        got = cc.load_cached(key_a)
        check("角色卡：存过就能读回来", bool(got) and got["attributes"][0]["key"] == "力量")
        (tmpdir / f"{key_a}.json").write_text("{ 这不是 JSON", encoding="utf-8")
        check("角色卡：缓存坏了当没缓存（重新分析一次，不崩）", cc.load_cached(key_a) is None)
        (tmpdir / f"{key_a}.json").write_text(
            json.dumps({"card": {"required": True, "attributes": [{"key": "x", "min": 5, "max": 5}]}}),
            encoding="utf-8",
        )
        fixed = cc.load_cached(key_a)
        check(
            "角色卡：缓存读出来也要过一遍规范化",
            bool(fixed) and fixed["attributes"][0]["min"] < fixed["attributes"][0]["max"],
            str(fixed)[:90],
        )
    finally:
        cc.CARD_DIR = real_dir

    # ---- 给模型看的剧本摘要 ----
    digest = cc.script_digest(totsk_store)
    check("剧本摘要：带剧本名与检定技能清单", "《" in digest and "检定技能" in digest, digest[:60])
    notes = [((s.get("dm_notes") or "")[:15]) for s in totsk_store.scenes.values()]
    notes = [n for n in notes if len(n) > 15]
    check("剧本摘要：不含 DM 内幕（设计角色卡不需要主持人的底牌）", not any(n in digest for n in notes))

    # ---- 分析：模型给了就洗，没给就明确失败（不造假卡） ----
    reply = json.dumps(
        {"required": True, "reason": "有战斗", "total_points": 4,
         "attributes": [{"key": "力量", "min": 8, "base": 10, "max": 16}],
         "skill_map": {"战斗": "力量"}},
        ensure_ascii=False,
    )
    res = await analyze_character_card(store_now, FakeProvider([reply]))
    check("角色卡：接上模型就能分析出卡", res.source == "llm" and res.data.get("required") is True, str(res.data)[:100])
    bad = await analyze_character_card(store_now, FakeProvider(["我觉得这个本很棒"]))
    check("角色卡：模型胡说时不造假卡（明确失败）", bad.source == "failed" and not bad.data, str(bad.error)[:80])
    none = await analyze_character_card(store_now, None)
    check("角色卡：没有模型就没有卡", none.source == "failed" and none.stopped == "no_provider", str(none)[:80])

    # ---- 检定：加值真的加在骰面上 ----
    real_roll = dice.roll
    try:
        dice.roll = lambda d=20: 10
        r = dice.roll_check("战斗", 12, mod=2)
        check("检定：d20 + 加值 与 DC 比（10+2 ≥ 12 成功）", r["total"] == 12 and r["success"] is True and r["mod"] == 2, str(r))
        r2 = dice.roll_check("战斗", 12, mod=1)
        check("检定：差一点就是失败（10+1 < 12）", r2["total"] == 11 and r2["success"] is False, str(r2))
        r3 = dice.roll_check("战斗", 10)
        check("检定：不给加值时行为与以前完全一致", r3["total"] == 10 and r3["mod"] == 0 and r3["success"] is True, str(r3))
    finally:
        dice.roll = real_roll


async def run_live_card():
    print("== 实时校验：角色卡（提交 / 越界拒绝 / 开局后锁定） ==")
    try:
        import websockets
    except ImportError:
        print("  [SKIP] 未安装 websockets，跳过")
        return

    from server import main as M
    from server import character_card as cc

    url = ws_url()
    tag = str(int(time.time() * 1000) % 100000)
    code, plain = "CRD" + tag, "NCD" + tag
    opened = []

    async def dial():
        w = await websockets.connect(url, proxy=None)
        opened.append(w)
        return w

    card = cc.normalize_card(
        {
            "required": True, "reason": "有战斗与学识检定", "total_points": 6,
            "attributes": [
                {"key": "力量", "desc": "搬、劈、扛", "min": 8, "base": 10, "max": 16},
                {"key": "学识", "desc": "识破机关", "min": 10, "base": 10, "max": 18},
            ],
            "skill_map": {"战斗": "力量", "调查": "学识"},
        }
    )
    no_card = cc.normalize_card({"required": False, "reason": "纯叙事，没有检定"})

    # 这个本走的是服务端当前的活动剧本；那个本显式指到 sample_script.json（换个缓存键）。
    plain_store = ScriptStore(SCRIPT, label="sample_script.json")
    seeded = [
        (cc.card_key(M.store), card, M.store.title),
        (cc.card_key(plain_store), no_card, plain_store.title),
    ]
    # 备份真实缓存 → 用完还原。否则这里塞进去的假卡会一直生效（缓存按剧本内容命中）。
    backups = {}
    for key, payload, title in seeded:
        p = cc.cache_path(key)
        backups[key] = p.read_text(encoding="utf-8") if p.exists() else None
        cc.save_cached(key, payload, title=title)

    try:
        a = await dial()
        await a.send(
            json.dumps(
                {"type": "join", "name": "甲", "room": code, "character": {"cls": "战士"}},
                ensure_ascii=False,
            )
        )
        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        cs = (wa or {}).get("card_state") or {}
        check(
            "角色卡实时：welcome 带上卡片状态",
            cs.get("status") in ("pending", "ready", "failed"),
            str(cs)[:90],
        )

        # 连带进来就会开始分析（这里命中的是刚写好的缓存，所以是秒回）。
        snap = await recv_until(
            a, lambda m: m["type"] == "character_schema" and m.get("status") == "ready", timeout=10
        )
        check("角色卡实时：进房后分析结果广播到等待室", bool(snap), str(snap)[:110])
        check("角色卡实时：这个本需要填卡", (snap or {}).get("needs_card") is True, str(snap)[:100])
        check(
            "角色卡实时：下发的卡不含 skill_map（内幕不出服务端）",
            bool(snap) and "skill_map" not in (snap.get("card") or {}),
            str(snap)[:120],
        )
        check(
            "角色卡实时：属性带上区间与基础值（界面要靠它画加减号）",
            bool(snap) and (snap["card"]["attributes"][0]["max"], snap["card"]["attributes"][0]["base"]) == (16, 10),
            str((snap or {}).get("card"))[:120],
        )

        await a.send(json.dumps({"type": "character", "attrs": {"力量": 14, "学识": 12}}, ensure_ascii=False))
        ok = await recv_until(a, lambda m: m["type"] in ("character_ok", "character_rejected"))
        check("角色卡实时：提交合法卡通过", (ok or {}).get("type") == "character_ok", str(ok)[:120])
        check("角色卡实时：回执带一行摘要", (ok or {}).get("summary") == "力量 14 · 学识 12", str((ok or {}).get("summary")))
        st = await recv_until(a, lambda m: m["type"] == "state")
        me = next(
            (p for p in ((st or {}).get("state") or {}).get("players", []) if p.get("name") == "甲"),
            None,
        )
        check(
            "角色卡实时：属性进了服务端权威状态",
            bool(me) and ((me.get("character") or {}).get("attrs") or {}).get("力量") == 14,
            str(me)[:120],
        )

        await a.send(json.dumps({"type": "character", "attrs": {"力量": 30, "学识": 10}}, ensure_ascii=False))
        bad = await recv_until(a, lambda m: m["type"] in ("character_ok", "character_rejected"))
        check("角色卡实时：越界的提交被拒", (bad or {}).get("type") == "character_rejected", str(bad)[:120])

        await a.send(json.dumps({"type": "character", "attrs": {"力量": 16, "学识": 18}}, ensure_ascii=False))
        over = await recv_until(a, lambda m: m["type"] in ("character_ok", "character_rejected"))
        check("角色卡实时：点数超了被拒", (over or {}).get("type") == "character_rejected", str(over)[:120])

        # 等待中改来改去是正常的；开局之后就不许再动了
        await a.send(json.dumps({"type": "character", "attrs": {"力量": 12, "学识": 10}}, ensure_ascii=False))
        again = await recv_until(a, lambda m: m["type"] in ("character_ok", "character_rejected"))
        check("角色卡实时：等待中还能改", (again or {}).get("type") == "character_ok", str(again)[:120])

        await start_room(a)
        await a.send(json.dumps({"type": "character", "attrs": {"力量": 16, "学识": 18}}, ensure_ascii=False))
        locked = await recv_until(a, lambda m: m["type"] in ("character_ok", "character_rejected"))
        check("角色卡实时：开局后不能再改", (locked or {}).get("type") == "character_rejected", str(locked)[:120])

        # ---- 不需要角色卡的剧本：整块不该出现，硬提交要被回绝 ----
        b = await dial()
        await b.send(
            json.dumps(
                {"type": "join", "name": "乙", "room": plain,
                 "script": "sample_script.json", "character": {"cls": "法师"}},
                ensure_ascii=False,
            )
        )
        wb = await recv_until(b, lambda m: m["type"] == "welcome")
        check("角色卡实时：不需要卡的房间也带状态", isinstance((wb or {}).get("card_state"), dict), str(wb)[:90])
        plain_snap = await recv_until(
            b, lambda m: m["type"] == "character_schema" and m.get("status") == "ready", timeout=10
        )
        check(
            "角色卡实时：不需要卡的剧本 needs_card=false",
            (plain_snap or {}).get("needs_card") is False,
            str(plain_snap)[:100],
        )
        await b.send(json.dumps({"type": "character", "attrs": {"力量": 10}}, ensure_ascii=False))
        rej = await recv_until(b, lambda m: m["type"] in ("character_ok", "character_rejected"))
        check("角色卡实时：不需要卡时提交被回绝", (rej or {}).get("type") == "character_rejected", str(rej)[:120])
    finally:
        for w in opened:
            try:
                await w.close()
            except Exception:  # noqa: BLE001
                pass
        for key, _payload, _title in seeded:
            p = cc.cache_path(key)
            raw = backups.get(key)
            try:
                if raw is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(raw, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


async def main():
    global PASS, FAIL
    unit_only = "--unit" in sys.argv
    await run_unit()
    await run_unit_totsk()
    await run_unit_agents()
    await run_unit_npc()
    await run_unit_chronicle()
    await run_unit_scripts()
    await run_unit_lobby()
    await run_unit_card()
    await run_unit_engine()
    if not unit_only:
        await run_live()
        await run_live_version()
        await run_live_latejoin()
        await run_live_configure()
        await run_live_model_test()
        await run_live_scripts()
        await run_live_room_script()
        await run_live_lobby()
        await run_live_card()
        await run_live_npc()
        await run_live_chronicle()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
