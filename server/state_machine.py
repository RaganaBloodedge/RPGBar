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

    def add_player(self, name, character):
        p = Player(name, character, self.turn)
        self.players[name] = p
        return p

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
