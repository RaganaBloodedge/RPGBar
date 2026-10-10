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

from server import __version__
from server.agents import (
    AdvisorAgent,
    AdvisorContext,
    AssistantAgent,
    NarratorAgent,
    NarratorContext,
    Task,
    ToolAgent,
    describe_agents,
)
from server import dice
from server.dice import seed as dice_seed
from server.dm import DM
from server.llm import ToolCallingUnsupported, build_provider
from server.npc import ARCHETYPES, NpcService, PersonaCard, infer_archetype
from server.rag import ScriptStore
from server.script_loader import build_script_prompt, parse_script
from server.state_machine import GameState

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

    # DM（脚本化兜底）：行动 → 检定 / 出口
    dm = DM(store, None)
    state3 = GameState(store, "gate")
    state3.add_player("A", {"name": "A", "cls": "战士"})
    evs = await dm.handle_action(state3, "A", "说服老周")
    types = [e["type"] for e in evs]
    check("检定行动触发 dice 事件", "dice" in types, str(types))
    evs2 = await dm.handle_action(state3, "A", "推开铁门进入门厅")
    check("出口行动推进场景", state3.current_scene == "hall", state3.current_scene)
    opts = await dm.suggest(state3)
    check("助手建议非空", isinstance(opts, list) and len(opts) > 0, str(opts))

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

    intro = await dm.introduce(prog, "梅林", {"cls": "法师"})
    check("引导旁白非空", bool(intro and intro.strip()))
    check("引导旁白含新玩家名字与职业", "梅林" in intro and "法师" in intro, intro[:60])
    check("引导旁白复述此前行程", "门厅" in intro, intro[:80])
    check("引导旁白复述已获线索", "暗门" in intro, intro[:120])

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
    dice_mod.roll_check = lambda skill, dc: {"skill": skill, "dc": dc, "roll": 20, "success": True}
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
        check("蛇王墓的中途加入旁白非空", bool(intro and intro.strip()), intro[:60])
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

    # --- 9. 无结构文本也能真跑起来（无模型 → 脚本化提示）---
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
        await NarratorAgent(store, fake4).run(ctx4, "检查")
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
        "小助手：读场景不下发 DM 内幕",
        "dm_notes" not in json.dumps(adv_tools[0].handler(AdvisorContext(state=GameState(store, start))), ensure_ascii=False),
        "",
    )

    # --- 6. 小助手：接了模型走工具循环，没接走脚本化 ---
    s6 = GameState(store, start)
    s6.add_player("A", {"cls": "法师"})
    adv_fake = FakeProvider([_reply(tool_call("finish", {"options": ["检查石门", "观察四周", "听一听"]}))])
    r6 = await AdvisorAgent(store, adv_fake).suggest(AdvisorContext(state=s6, player_name="A"))
    check("小助手：模型给的建议入列", r6.data.get("options") == ["检查石门", "观察四周", "听一听"], str(r6.data))
    check("小助手：用了 finish 工具", r6.used_tools == ["finish"], str(r6.used_tools))
    r6b = await AdvisorAgent(store, None).suggest(AdvisorContext(state=s6, player_name="A"))
    check(
        "小助手：无模型时给脚本化建议",
        bool(r6b.data.get("options")) and r6b.source == "scripted",
        f"{r6b.data} / {r6b.source}",
    )

    # --- 7. 无 provider：DM 退化为确定性流水线，玩法不变 ---
    s7 = GameState(store, start)
    s7.add_player("A", {"cls": "战士"})
    n7 = NarratorAgent(store, None)
    ctx7 = NarratorContext(state=s7, player_name="A")
    r7 = await n7.run(ctx7, "我环顾四周，警惕地观察")
    check("无模型：stopped=no_provider", r7.stopped == "no_provider", r7.stopped)
    check("无模型：交给确定性流水线（产出可广播的事件）", bool(ctx7.events), str(ctx7.events)[:100])
    check("无模型：不经过 provider.chat", not isinstance(n7.provider, FakeProvider) and n7.provider is None, str(n7.provider))

    # --- 8. 模型不支持工具调用 → 自动降级为单轮 JSON 协议 ---
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
    check("降级：模型不支持工具 → 单轮 JSON 生效", s8.current_scene == first_exit, s8.current_scene)
    check("降级：旁白取自 JSON", "环顾四周" in r8.text, r8.text)
    check("降级：stopped=no_tools / source=degraded", r8.stopped == "no_tools" and r8.source == "degraded", f"{r8.stopped}/{r8.source}")

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
    check("对外状态：留空即 scripted", pub["advisor"].get("ready") is False and pub["advisor"].get("mode") == "scripted", str(pub["advisor"]))

    # --- 12. 通用任务引擎：单轮 JSON 任务（无工具）走「校验 → 兜底」 ---
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
                    fallback=lambda a, c: {"items": ["兜底"]},
                    validate=lambda a, d, c: {"items": [str(x) for x in (d.get("items") or [])][:3]},
                )
            )

    r_none = await _DummyTaskAgent(None).run_task("dummy", None)
    check(
        "引擎：无模型的任务 → 兜底数据 + scripted",
        r_none.data.get("items") == ["兜底"] and r_none.source == "scripted",
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
        "引擎：模型输出不合规 → 回落兜底并标 degraded",
        r_bad.data.get("items") == ["兜底"] and r_bad.source == "degraded",
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


async def recv_until(ws, pred, timeout=6.0):
    end = time.time() + timeout
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

        # 亚瑟推进场景（出口推进，无骰子，确定性）
        await a.send(json.dumps({"type": "action", "text": acts["move"]}, ensure_ascii=False))
        st = await recv_until(a, lambda m: m["type"] == "state" and m["state"]["current_scene"] == acts["scene"])
        check("联机：出口推进到下一场景", bool(st), f"未到达 {acts['scene']}")

        # 亚瑟触发检定（必有 dice 事件）
        await a.send(json.dumps({"type": "action", "text": acts["check"]}, ensure_ascii=False))
        dice_ev = await recv_until(a, lambda m: m["type"] == "dice")
        check("联机：检定触发骰子事件", bool(dice_ev), "无 dice 事件")

        # 梅林请求助手建议
        await b.send(json.dumps({"type": "suggest"}, ensure_ascii=False))
        sug = await recv_until(b, lambda m: m["type"] == "suggestions")
        check("联机：助手建议返回", bool(sug) and len(sug.get("options", [])) > 0, str(sug)[:80])

        # 梅林自由发挥（触发 DM 旁白）
        await b.send(json.dumps({"type": "action", "text": "我环顾四周，警惕地观察"}, ensure_ascii=False))
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
    end = time.time() + timeout
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

        # 先到者推进剧情
        await a.send(json.dumps({"type": "action", "text": acts["move"]}, ensure_ascii=False))
        st = await recv_until(a, lambda m: m["type"] == "state" and m["state"]["current_scene"] == acts["scene"])
        check("联机：先到者推进场景", bool(st), f"未到达 {acts['scene']}")
        await a.send(json.dumps({"type": "action", "text": acts["check"]}, ensure_ascii=False))
        await recv_until(a, lambda m: m["type"] == "dice")

        # 新玩家中途加入同一房间
        b = await websockets.connect(url, proxy=None)
        await b.send(json.dumps({"type": "join", "name": "迟到者", "room": room, "character": {"cls": "法师"}}, ensure_ascii=False))
        b_msgs = await collect(b, 6)
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
            isinstance(fd, list)
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
        check("联机：默认留空即脚本化", agents.get("dm", {}).get("ready") is False, str(agents.get("dm"))[:90])

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
        check("NPC 实时：welcome 同步本房间 NPC 列表", any(n.get("name") == "老周" for n in npcs), str(npcs)[:140])
        check("NPC 实时：NPC 卡带身份", any(n.get("role") for n in npcs), str(npcs)[:140])

        await a.send(json.dumps({"type": "action", "text": "我打了老周一拳"}, ensure_ascii=False))
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


async def main():
    global PASS, FAIL
    unit_only = "--unit" in sys.argv
    await run_unit()
    await run_unit_totsk()
    await run_unit_agents()
    await run_unit_npc()
    await run_unit_scripts()
    if not unit_only:
        await run_live()
        await run_live_version()
        await run_live_latejoin()
        await run_live_configure()
        await run_live_model_test()
        await run_live_scripts()
        await run_live_room_script()
        await run_live_npc()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
