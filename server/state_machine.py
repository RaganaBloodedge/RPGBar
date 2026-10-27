"""剧情状态机：服务端权威状态，管场景 / flag / 出口 / 检定。"""


class Player:
    def __init__(self, name, character, joined_at_turn=0):
        self.name = name
        self.character = character or {}
        self.hp = self.character.get("hp", 10)
        self.joined_at_turn = joined_at_turn  # 加入时的回合数，>0 即中途加入

    def to_dict(self):
        return {
            "name": self.name,
            "character": self.character,
            "hp": self.hp,
            "joined_at_turn": self.joined_at_turn,
        }


class GameState:
    def __init__(self, store, start_scene):
        self.store = store
        self.players = {}
        self.flags = set()
        self.current_scene = start_scene
        self.visited = [start_scene]  # 走过的场景（有序、去重），用于给新人复述行程
        self.history = []
        self.turn = 0
        self.ended = False
        self.fact_events = []  # 服务端确认的结构化事实；旁白不写入这里

    def commit_event(self, event_type, *, actor="", target="", success=None,
                     allow_generated=False, text="", facts_added=None,
                     facts_removed=None):
        """验证并提交一条事实事件。所有场景/flag 变化经此入口完成。

        事件使用剧本提供的出口、检定和 flag；没有特定剧本的词表。
        沙盒生成的地点由调用方先登记，再用 allow_generated 明确提交。
        """
        before = self.current_scene
        flags_before = set(self.flags)
        if event_type == "scene_entered":
            current = self.current() or {}
            exit_ = next((x for x in current.get("exits", []) if x.get("to") == target), None)
            if not allow_generated:
                if exit_ is None:
                    raise ValueError(f"{target} 不是当前场景出口")
                if exit_.get("condition") and exit_["condition"] not in self.flags:
                    raise ValueError(f"出口条件未满足：{exit_['condition']}")
            if not self.enter(target):
                raise ValueError(f"无法进入场景：{target}")
            self.turn += 1
        elif event_type == "check_resolved":
            check = next((c for c in (self.current() or {}).get("checks", [])
                          if c.get("id") == target), None)
            if check is None and not allow_generated:
                raise ValueError(f"当前场景没有检定：{target}")
            if success is None:
                raise ValueError("检定事件缺少成功/失败结果")
            if success and check and check.get("success_flag"):
                self.flags.add(check["success_flag"])
        elif event_type == "flag_added":
            if target not in self.store.flag_ids:
                raise ValueError(f"剧本未声明 flag：{target}")
            self.flags.add(target)
        elif event_type in {"entity_declared", "time_advanced", "thread_opened",
                            "thread_closed", "npc_introduced", "relation_changed",
                            "world_changed"}:
            # 世界层的事实由 World 模块先完成确定性写入，这里统一登记事件。
            pass
        else:
            raise ValueError(f"未知事实事件：{event_type}")
        event = {
            "id": f"event_{len(self.fact_events) + 1:06d}",
            "type": event_type, "turn": self.turn, "actor": actor,
            "target": target, "scene_before": before,
            "scene_after": self.current_scene,
            "facts_added": list(facts_added) if facts_added is not None else sorted(self.flags - flags_before),
            "facts_removed": list(facts_removed or []), "source": "server", "text": text,
        }
        if success is not None:
            event["success"] = bool(success)
        self.fact_events.append(event)
        return event

    def add_player(self, name, character):
        p = Player(name, character, self.turn)
        self.players[name] = p
        return p

    def rollback_event(self, event_id: str) -> bool:
        """撤销尚未广播的最后一个世界事实事件。

        只允许回滚最后一条，避免把已经进入 Chronicle/客户端的事实改写。
        用于跨 World SQLite 写入失败时保持事件流和世界层一致。
        """
        if not self.fact_events or self.fact_events[-1].get("id") != event_id:
            return False
        self.fact_events.pop()
        return True

    def remove_player(self, name):
        self.players.pop(name, None)

    # ---- 进度判定与回顾（供中途加入使用）----
    def is_in_progress(self) -> bool:
        """本局是否已经开局（有新玩家加入时，用于决定是否需要补剧情）。"""
        return (
            self.turn > 0
            or bool(self.flags)
            or len(self.visited) > 1
            or bool(self.history)
            or self.ended
        )

    def scene_path(self) -> list:
        """已走过的场景标题序列。"""
        titles = []
        for sid in self.visited:
            s = self.store.get_scene(sid)
            titles.append(s["title"] if s else sid)
        return titles

    def recap(self, max_history: int = 8) -> dict:
        """结构化剧情回顾：行程 / 线索 / 最近动态，供 DM 给新玩家补课。"""
        cur = self.current()
        return {
            "scene_path": self.scene_path(),
            "current_scene": self.current_scene,
            "scene_title": cur["title"] if cur else "",
            "location": cur["location"] if cur else "",
            "flags": [
                {"id": f, "label": self.store.flag_label(f)} for f in sorted(self.flags)
            ],
            "turn": self.turn,
            "recent": self.history[-max_history:],
        }

    def current(self):
        return self.store.get_scene(self.current_scene)

    def can_enter(self, scene) -> bool:
        for f in scene.get("flags_required", []):
            if f not in self.flags:
                return False
        return True

    def enter(self, scene_id) -> bool:
        scene = self.store.get_scene(scene_id)
        if not scene or not self.can_enter(scene):
            return False
        self.current_scene = scene_id
        if scene_id not in self.visited:
            self.visited.append(scene_id)
        for f in scene.get("flags_set", []):
            self.flags.add(f)
        return True

    def set_flag(self, f):
        if f:
            self.flags.add(f)

    def log(self, entry):
        self.history.append(entry)
        if len(self.history) > 40:
            self.history = self.history[-40:]

    def recent_history_text(self, n=12):
        return "\n".join(self.history[-n:])

    def snapshot(self):
        cur = self.current()
        return {
            "current_scene": self.current_scene,
            "scene_title": cur["title"] if cur else "",
            "location": cur["location"] if cur else "",
            "flags": sorted(self.flags),
            "flag_details": [
                {"id": f, "label": self.store.flag_label(f)} for f in sorted(self.flags)
            ],
            "players": [p.to_dict() for p in self.players.values()],
            "turn": self.turn,
            "ended": self.ended,
        }
