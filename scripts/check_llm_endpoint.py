"""接一个 OpenAI 兼容端点，自检 LLM 节点：协议通不通、护栏挡不挡得住。

给"我要接自己的 4B 权重"这件事用的。跑一遍就知道三件事：

1. **协议层**能不能通（`BaseChatModel` 的请求格式对不对、并发有没有问题）；
2. **护栏**是不是真的在拦（编造证据 / 非法类别 / 空字段会不会被判废）；
3. **成本面**：一次调用大概多少 token（决定 4B 值不值得接）。

两种用法：

```bash
# 内置 mock：不需要任何模型，验证协议与护栏（CI 里应该常跑）
python scripts/check_llm_endpoint.py --mock

# 真端点：指向你本地跑起来的 4B（vLLM / llama.cpp server / ollama 的 OpenAI adapter 都行）
python scripts/check_llm_endpoint.py --endpoint http://localhost:8000/v1 --model qwen3-4b
```

**它测的是"节点能不能用"，不是"模型好不好"。** 模型的分类准不准，要用
`data/kb/*/report.json → spec.unclassified_count` 与人工抽样来看，那不是一个脚本能判的。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compiler import spec as spec_mod  # noqa: E402
from compiler.graph.llm_classify import review_blocks  # noqa: E402
from compiler.outline import Block  # noqa: E402

RULE_TEXT = (
    "战斗轮按 DEX 顺序进行。每轮大约十秒。若有人突袭，则守秘人可以要求敏捷检定，"
    "失败者本轮不得行动。同一轮内，调查员可以移动并攻击，但不能同时进行两次攻击。"
)
LORE_TEXT = (
    "在1925 年的阿卡姆，米斯卡托尼克大学仍保存着几卷用拉丁文写成的抄本。"
    "当地人对那座图书馆的数量众说纷纭，但没人能说清地窖里究竟还留着什么。"
    "据老图书管理员说，其中一卷在二十年前被人借走后就没有归还，登记簿上的签名早已模糊不清。"
)

CASE_OK = {"kind": "rule", "subtype": "战斗", "evidence": "战斗轮按 DEX 顺序进行", "confidence": 0.72,
           "reason": "含顺序与检定判定，属规则"}
CASE_LORE = {"kind": "lore", "subtype": "设定", "evidence": "在1925 年的阿卡姆", "confidence": 0.68,
             "reason": "叙述性段落，讲世界设定"}
CASE_FABRICATE = {"kind": "rule", "subtype": "战斗", "evidence": "此句原文中绝对不存在，纯属编造",
                  "confidence": 0.95, "reason": "编的"}
CASE_ILLEGAL = {"kind": "weapon", "subtype": "战斗", "evidence": "战斗轮按 DEX 顺序进行", "confidence": 0.99,
                "reason": "类别不在枚举里"}


class MockState:
    mode = "ok"
    calls = 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        prompt = ""
        for m in req.get("messages") or []:
            prompt += str(m.get("content") or "")
        body = _code_block(prompt)

        MockState.calls += 1
        if MockState.mode == "ok":
            # 像个"判对了"的模型：按内容选类别，证据取原文片段
            base = CASE_LORE if "阿卡姆" in body else CASE_OK
            payload = dict(base, evidence=(body[:12] or base["evidence"]))
        elif MockState.mode == "fabricate":
            payload = CASE_FABRICATE
        else:
            payload = CASE_ILLEGAL

        out = {
            "id": "mock",
            "object": "chat.completion",
            "created": 0,
            "model": req.get("model") or "mock",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": len(prompt) // 2, "completion_tokens": 40, "total_tokens": len(prompt) // 2 + 40},
        }
        raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _code_block(prompt: str) -> str:
    i = prompt.find("```")
    if i < 0:
        return ""
    j = prompt.find("\n", i + 3)
    k = prompt.find("```", j + 1)
    if j < 0 or k < 0:
        return ""
    return prompt[j + 1 : k].strip()


def _block(bid: str, text: str) -> Block:
    return Block(
        id=bid, section_id="s1", order=0, page=100, page_end=100,
        title="待判块", text=text,
        meta={"chapter": "第六章 战斗", "section": "6.2 常规接触战斗"},
    )


def _run_case(mode: str, blocks: list[Block], expect_accepted: int) -> tuple[bool, str]:
    MockState.mode = mode
    classes = {b.id: {"kind": "unclassified", "subtype": "", "confidence": 0.0, "by": "spec"} for b in blocks}
    changed, rejected = review_blocks(list(blocks), classes)
    ok = len(changed) == expect_accepted
    detail = (
        f"mode={mode:10s} 采纳 {len(changed)} 判废 {rejected} "
        f"（期望采纳 {expect_accepted}）"
    )
    if changed:
        b0 = next(b for b in blocks if b.id in changed)
        detail += f"｜例：{b0.id} → {classes[b0.id]['kind']}/{classes[b0.id]['subtype']}"
    if rejected:
        rej = next((classes[b.id].get("llm_rejected") for b in blocks if classes[b.id].get("llm_rejected")), None)
        detail += f"｜判废原因：{rej}"
    return ok, detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM 节点自检（协议 + 护栏）")
    ap.add_argument("--endpoint", default=None, help="OpenAI 兼容 base_url，例如 http://localhost:8000/v1")
    ap.add_argument("--model", default="mock-4b")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--mock", action="store_true", help="用内置 mock 服务器（不需要模型）")
    args = ap.parse_args(argv)

    if args.mock:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{srv.server_port}/v1"
        print(f"内置 mock 已启动：{endpoint}\n")
    elif args.endpoint:
        endpoint = args.endpoint.rstrip("/")
    else:
        print("请给 --mock（自检协议与护栏）或 --endpoint（接你自己的模型）")
        return 2

    os.environ["RPGBAR_LLM"] = f"{args.model}@{endpoint}|{args.api_key or 'sk-local'}"

    from compiler.graph.llm_classify import get_default_model

    model = get_default_model()
    if model is None:
        print("无法构造模型：缺 langchain-openai，或端点配置无效")
        return 2

    blocks = [_block("b1", RULE_TEXT), _block("b2", LORE_TEXT)]
    results: list[tuple[bool, str]] = []

    results.append(_run_case("ok", blocks, expect_accepted=len(blocks)))
    if args.mock:
        results.append(_run_case("fabricate", blocks, expect_accepted=0))
        results.append(_run_case("illegal", blocks, expect_accepted=0))

    # 护栏单测（不依赖网络）：直接喂编造的判定
    v, probs = spec_mod.check_llm_verdict(CASE_FABRICATE, RULE_TEXT, {})
    results.append((v["kind"] == "unclassified", f"护栏:编造证据 → {v['kind']}｜{probs[:1]}"))
    v2, probs2 = spec_mod.check_llm_verdict(CASE_ILLEGAL, RULE_TEXT, {})
    results.append((v2["kind"] == "unclassified", f"护栏:非法类别 → {v2['kind']}｜{probs2[:1]}"))

    print()
    all_ok = True
    for ok, detail in results:
        print(("  ✓ " if ok else "  ✗ ") + detail)
        all_ok &= ok

    if args.mock:
        print(f"\n共 {MockState.calls} 次模型调用（含并发）")
    print()
    print("结论：" + ("协议通、护栏生效 —— 这个端点可以接进 llm_review 节点" if all_ok
                     else "有问题：上面标 ✗ 的项必须先修"))
    print("注意：这只证明\"节点能用\"。模型判得准不准，要看 report.json 的 unclassified_count 与人工抽样。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
