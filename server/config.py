"""配置加载：config.example.json 默认值 + config.json 本地覆盖 + 环境变量。"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_PATH = BASE_DIR / "config.example.json"

DEFAULTS = {
    "llm": {
        "provider": "mock",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.8,
    },
    "server": {"host": "127.0.0.1", "port": 8000},
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

    # 有 key 即启用 openai 兼容 provider
    if cfg["llm"].get("api_key"):
        cfg["llm"]["provider"] = "openai"
    return cfg
