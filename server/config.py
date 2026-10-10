"""配置加载：config.example.json 默认值 + config.json 本地覆盖 + 环境变量。

路径约定：
- RESOURCE_DIR：只读资源（web/、scripts/、config.example.json）。
  源码运行 = 项目根；打包运行 = PyInstaller 解包目录（sys._MEIPASS）。
- USER_DIR：可写配置（config.json）所在目录。
  源码运行 = 项目根；打包运行 = exe 所在目录（用户可编辑）。
"""
import json
import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_dir() -> Path:
    if _is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def user_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


RESOURCE_DIR = resource_dir()
USER_DIR = user_dir()
CONFIG_PATH = USER_DIR / "config.json"
EXAMPLE_PATH = RESOURCE_DIR / "config.example.json"

DEFAULTS = {
    # 模型槽位。kind: off（留空，走脚本化兜底）/ cloud（云 API，需 api_key）/ local（本地模型）
    # 默认全部留空 —— 游戏照常可玩，配好任意一个即自动启用对应 Agent。
    # - dm：主机端 DM 大模型（推进剧情）。
    # - advisor：玩家端小助手小模型（只读，给行动建议）。
    # - assistant：主机端小助手小模型（生成 NPC 人物卡、算反应权重、滚动摘要）——
    #   与 advisor 是同一个引擎，只是跑在主机、承担房间级任务。3B 量级足够。
    "models": {
        "dm": {
            "kind": "off",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "model": "deepseek-chat",
            "temperature": 0.8,
        },
        "advisor": {
            "kind": "off",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "",
            "model": "local-model",
            "temperature": 0.7,
        },
        "assistant": {
            "kind": "off",
            "base_url": "http://127.0.0.1:8081/v1",
            "api_key": "",
            "model": "qwen2.5-3b-instruct",
            "temperature": 0.6,
        },
    },
    # NPC 子系统：是否允许用模型为次要 NPC 生成人物小传（关掉则走原型兜底表）
    "npc": {"auto_persona": True},
    # 剧情档案：把已输出的主线全文落到本地只增 JSONL，并提供 BM25 + 向量混合检索。
    # summary_every：每积累这么多条新记录，就让主机小助手滚动摘要一次（不接模型时用确定性兜底）。
    # 嵌入模型（bge-small-zh-v1.5）缺失时自动退化成纯 BM25，不影响游玩。
    "memory": {"chronicle": True, "summary_every": 8},
    "server": {"host": "0.0.0.0", "port": 8000},
    # 剧本文件（相对 RESOURCE_DIR；也可写绝对路径）
    "script": "scripts/sample_script.json",
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env_slot(slot: dict, prefix: str) -> None:
    """按环境变量填充一个模型槽位：RPGBAR_<PREFIX>_API_KEY / _BASE_URL / _MODEL / _KIND。"""
    env = os.environ
    if env.get(f"{prefix}_API_KEY"):
        slot["api_key"] = env[f"{prefix}_API_KEY"]
        if (slot.get("kind") or "off") == "off":
            slot["kind"] = "cloud"
    if env.get(f"{prefix}_BASE_URL"):
        slot["base_url"] = env[f"{prefix}_BASE_URL"]
    if env.get(f"{prefix}_MODEL"):
        slot["model"] = env[f"{prefix}_MODEL"]
    if env.get(f"{prefix}_KIND"):
        slot["kind"] = env[f"{prefix}_KIND"]


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception:
            pass

    # 旧版（≤0.6）的 llm 段：若还在用，自动迁移到 models.dm，避免升级后静默失效
    legacy = cfg.pop("llm", None)
    if isinstance(legacy, dict) and legacy.get("api_key"):
        dm = cfg["models"]["dm"]
        if (dm.get("kind") or "off") == "off":
            dm["kind"] = "cloud"
            dm["api_key"] = legacy["api_key"]
            dm["base_url"] = legacy.get("base_url", dm["base_url"])
            dm["model"] = legacy.get("model", dm["model"])

    _apply_env_slot(cfg["models"]["dm"], "RPGBAR_LLM")  # 兼容旧变量名
    _apply_env_slot(cfg["models"]["dm"], "RPGBAR_DM_MODEL")
    _apply_env_slot(cfg["models"]["advisor"], "RPGBAR_ADVISOR_MODEL")
    _apply_env_slot(cfg["models"]["assistant"], "RPGBAR_ASSISTANT_MODEL")
    if os.environ.get("RPGBAR_SCRIPT"):
        cfg["script"] = os.environ["RPGBAR_SCRIPT"]
    return cfg
