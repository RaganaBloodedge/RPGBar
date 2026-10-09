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
    "llm": {
        "provider": "mock",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.8,
    },
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


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception:
            pass

    env = os.environ
    if env.get("RPGBAR_LLM_API_KEY"):
        cfg["llm"]["api_key"] = env["RPGBAR_LLM_API_KEY"]
    if env.get("RPGBAR_LLM_BASE_URL"):
        cfg["llm"]["base_url"] = env["RPGBAR_LLM_BASE_URL"]
    if env.get("RPGBAR_LLM_MODEL"):
        cfg["llm"]["model"] = env["RPGBAR_LLM_MODEL"]
    if env.get("RPGBAR_SCRIPT"):
        cfg["script"] = env["RPGBAR_SCRIPT"]

    # 有 key 即启用 openai 兼容 provider
    if cfg["llm"].get("api_key"):
        cfg["llm"]["provider"] = "openai"
    return cfg
