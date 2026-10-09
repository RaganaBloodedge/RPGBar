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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.dice import seed as dice_seed
from server.dm import DM
from server.rag import ScriptStore
from server.state_machine import GameState

SCRIPT = Path(__file__).resolve().parent / "sample_script.json"
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
    ws_url = os.environ.get("RPGBAR_WS_URL", "ws://127.0.0.1:8000/ws")
    try:
        a = await websockets.connect(ws_url, proxy=None)
        b = await websockets.connect(ws_url, proxy=None)
    except Exception as e:
        print(f"  [SKIP] 无法连接服务器 {ws_url}：{e}")
        return

    try:
        await a.send(json.dumps({"type": "join", "name": "亚瑟", "room": room, "character": {"cls": "战士"}}, ensure_ascii=False))
        await b.send(json.dumps({"type": "join", "name": "梅林", "room": room, "character": {"cls": "法师"}}, ensure_ascii=False))

        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        wb = await recv_until(b, lambda m: m["type"] == "welcome")
        check("双客户端拿到 welcome", wa and wb, f"{bool(wa)}/{bool(wb)}")

        # 亚瑟进入门厅（出口推进，无骰子，确定性）
        await a.send(json.dumps({"type": "action", "text": "推开铁门进入门厅"}, ensure_ascii=False))
        st = await recv_until(a, lambda m: m["type"] == "state" and m["state"]["current_scene"] == "hall")
        check("联机：出口推进到门厅", bool(st), "未到达 hall")

        # 亚瑟调查挂毯（触发检定，必有 dice 事件）
        await a.send(json.dumps({"type": "action", "text": "调查挂毯"}, ensure_ascii=False))
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

    ws_url = os.environ.get("RPGBAR_WS_URL", "ws://127.0.0.1:8000/ws")
    # 独立房间码，保证「先到者」进的是全新房间（否则会误判为中途加入）
    room = "LATE" + str(int(time.time() * 1000) % 100000)
    try:
        a = await websockets.connect(ws_url, proxy=None)
    except Exception as e:
        print(f"  [SKIP] 无法连接服务器 {ws_url}：{e}")
        return

    b = None
    try:
        await a.send(json.dumps({"type": "join", "name": "先到者", "room": room, "character": {"cls": "战士"}}, ensure_ascii=False))
        wa = await recv_until(a, lambda m: m["type"] == "welcome")
        check("联机：首人加入 welcome.late=False", bool(wa) and wa.get("late") is False, str(wa))

        # 先到者推进剧情：进入门厅
        await a.send(json.dumps({"type": "action", "text": "推开铁门进入门厅"}, ensure_ascii=False))
        st = await recv_until(a, lambda m: m["type"] == "state" and m["state"]["current_scene"] == "hall")
        check("联机：先到者推进到门厅", bool(st), "未到达 hall")
        await a.send(json.dumps({"type": "action", "text": "调查挂毯"}, ensure_ascii=False))
        await recv_until(a, lambda m: m["type"] == "dice")

        # 新玩家中途加入同一房间
        b = await websockets.connect(ws_url, proxy=None)
        await b.send(json.dumps({"type": "join", "name": "迟到者", "room": room, "character": {"cls": "法师"}}, ensure_ascii=False))
        b_msgs = await collect(b, 6)
        types = [m["type"] for m in b_msgs]

        wb = next((m for m in b_msgs if m["type"] == "welcome"), None)
        check("联机：中途加入 welcome.late=True", bool(wb) and wb.get("late") is True, str(wb))
        check("联机：welcome 先于补课消息", bool(types) and types[0] == "welcome", str(types))

        cap = next((m for m in b_msgs if m["type"] == "recap"), None)
        check("联机：新玩家收到私有故事回顾", bool(cap), str(types))
        rc = (cap or {}).get("recap", {})
        check("联机：回顾含已走过的行程", "门厅" in (rc.get("scene_path") or []), str(rc.get("scene_path")))

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
    await run_live()
    await run_live_latejoin()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
