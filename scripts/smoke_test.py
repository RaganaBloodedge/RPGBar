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
from server.dice import seed as dice_seed
from server.dm import DM
from server.rag import ScriptStore
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
    """校验 /api/version（版本号是发版机制的一环，打包后也必须可用）。"""
    import httpx

    try:
        r = httpx.get(http_base() + "/api/version", timeout=8, trust_env=False)
        data = r.json()
        check("联机：/api/version 返回版本号", r.status_code == 200 and bool(data.get("version")), str(data))
        check("联机：/api/version 返回剧本名", bool(data.get("script")), str(data))
    except Exception as e:
        print(f"  [SKIP] /api/version 不可用：{e}")


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


async def main():
    global PASS, FAIL
    await run_unit()
    await run_unit_totsk()
    await run_live()
    await run_live_version()
    await run_live_latejoin()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
