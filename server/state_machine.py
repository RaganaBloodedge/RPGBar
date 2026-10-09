"""剧情状态机：服务端权威状态，管场景 / flag / 出口 / 检定。"""


class Player:
    def __init__(self, name, character):
        self.name = name
        self.character = character or {}
        self.hp = self.character.get("hp", 10)

    def to_dict(self):
        return {"name": self.name, "character": self.character, "hp": self.hp}


class GameState:
    def __init__(self, store, start_scene):
        self.store = store
        self.players = {}
        self.flags = set()
        self.current_scene = start_scene
        self.history = []
        self.turn = 0
        self.ended = False

    def add_player(self, name, character):
        self.players[name] = Player(name, character)

    def remove_player(self, name):
        self.players.pop(name, None)

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
            "players": [p.to_dict() for p in self.players.values()],
            "turn": self.turn,
            "ended": self.ended,
        }
