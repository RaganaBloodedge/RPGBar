"""LLM 复核节点：**只在规则判不了的地方用模型，并且不许它乱说。**

三个约束让它便宜且可靠：

1. **只喂"判不了的块"**。规则分类器已经能给 90%+ 的块定性，剩下 `unclassified` 与低置信的
   才进模型。2200 块的书通常只有几十块需要调用。
2. **规范与验收共用一份**（`spec.py`）：提示词由 `spec.build_prompt()` 生成，答案由
   `spec.check_llm_verdict()` 验收。模型编造的证据在原文里找不到 → 直接判废，
   不给它圆场的机会。
3. **模型是可替换的**。这里只依赖 `BaseChatModel` 接口，OpenAI 兼容端点、本地 vLLM、
   Ollama 的 OpenAI adapter 都能直接用；换模型不改这里一行代码。

默认不启用：`use_llm=False`。没有模型时整条 LLM 分支在图上根本不会被走到，
所以这个 Agent 可以在完全离线的机器上跑完一本书。
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from .. import spec as spec_mod

_MODEL_CACHE: dict[str, object] = {}

ENV_MODEL = "RPGBAR_LLM"
"""模型声明。两种写法：
- `gpt-4o-mini`（读 OPENAI_API_KEY / OPENAI_BASE_URL）
- `gpt-4o-mini@https://my-endpoint/v1|sk-xxx`（显式端点与密钥，便于接本地推理服务）
"""


def get_default_model():
    """按环境变量构造模型；没有配置就返回 None（调用方据此跳过 LLM 分支）。"""
    decl = (os.getenv(ENV_MODEL) or "").strip()
    if not decl:
        return None
    if decl in _MODEL_CACHE:
        return _MODEL_CACHE[decl]
    model = decl
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    if "@" in decl:
        model, rest = decl.split("@", 1)
        if "|" in rest:
            base_url, api_key = rest.split("|", 1)
        else:
            base_url = rest
    try:
        from langchain_openai import ChatOpenAI  # 可选依赖
    except Exception:
        return None
    # 本地 llama.cpp 端点：Qwen3 默认输出思维链，判别任务不需要 —— 每个字段都按
    # decode token 付费（约 20-100 tok/s），思维链能把单批耗时放大 3 倍以上。
    # 用 chat_template_kwargs 关掉；只对本地端点注入，云端 OpenAI API 会拒未知参数。
    # 注意必须走 extra_body：model_kwargs 会被拆成 create() 的顶层 kwargs，
    # openai 客户端校验未知名直接 TypeError（实测：200 块全部秒判废）。
    extra: dict = {}
    http_client = None
    if base_url and ("127.0.0.1" in base_url or "localhost" in base_url):
        extra = {"chat_template_kwargs": {"enable_thinking": False}}
        # ★ 本机端点必须绕开系统代理。Windows 的"Internet 选项"里开了代理时，
        # `urllib.request.getproxies()` 会把它带进 httpx（trust_env 默认 True），
        # 于是发往 127.0.0.1:8080 的请求先被代理接走、返回 **502** —— 表现为
        # "模型调用失败"，而 llama-server 的日志里一条请求都没有。
        # 上一版就是被这个坑掉的：2175 块里 1351 块因此静默回落规则层。
        # 本地端点本来就不该走代理，直接关掉 trust_env。
        try:
            import httpx

            http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(600.0, connect=10.0))
        except Exception:  # noqa: BLE001  httpx 装了就能用，装不上退回默认
            http_client = None
    llm = ChatOpenAI(
        model=model,
        temperature=0,
        base_url=base_url or None,
        api_key=api_key or "sk-local",  # 本地推理服务通常不校验，占位即可
        extra_body=extra or None,
        http_client=http_client,
    )
    _MODEL_CACHE[decl] = llm
    return llm


def _extract_json(text: str) -> dict | None:
    """模型常把 JSON 包在 ```json 里，或者前后带一句人话。只取第一个完整对象。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except Exception:
                    return None
    return None


def review_one(block, model) -> tuple[dict | None, list[str]]:
    """单块复核。返回 (校验后的判定, 问题列表)。"""
    path = f"{block.meta.get('chapter', '')} > {block.meta.get('section', '')}".strip(" >")
    prompt = spec_mod.build_prompt(block.text or "", path)
    try:
        msg = model.invoke(prompt)
    except Exception as e:  # 网络/额度问题不该让整本书编译失败
        return None, [f"模型调用失败：{type(e).__name__}: {e}"]
    raw = getattr(msg, "content", msg)
    if isinstance(raw, list):  # 多模态返回
        raw = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in raw)
    verdict = _extract_json(str(raw))
    if not verdict:
        return None, ["输出不是合法 JSON"]
    checked, problems = spec_mod.check_llm_verdict(verdict, block.text or "", block.meta)
    if problems:
        return None, problems
    return checked, []


def review_blocks(todo: list, classes: dict, model=None) -> tuple[set[str], int]:
    """批量复核（并发）。直接改写 `classes`，返回 (被采纳的 block_id 集合, 判废数)。

    **判废的块保持原样**（仍是 unclassified），不做任何兜底猜测 —— 宁可留空。
    """
    model = model or get_default_model()
    if model is None or not todo:
        return set(), 0

    def work(b):
        return b.id, *review_one(b, model)

    changed: set[str] = set()
    rejected = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for bid, verdict, problems in pool.map(work, todo):
            if verdict is None:
                rejected += 1
                cur = classes.get(bid) or {}
                cur["llm_rejected"] = problems[:2]
                classes[bid] = cur
                continue
            classes[bid] = {
                "kind": verdict["kind"],
                "subtype": verdict.get("subtype") or "",
                "confidence": float(verdict.get("confidence") or 0.75),
                "evidence": [verdict.get("evidence") or "", verdict.get("reason") or ""],
                "by": "llm",
            }
            changed.add(bid)
    return changed, rejected
