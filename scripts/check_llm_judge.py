"""端到端验证「LLM 判别」这条路：用 mock 端点跑一遍真实编译。

它验证的不是"模型准不准"（那要看真模型的报告），而是**这条链路是不是真的把判别权交给了模型**：

1. `by=llm` 的块占比 —— 证明模型在判，不是规则在判；
2. 校验拦得住 —— mock 一旦编造证据，该块就回落规则，库不被污染；
3. 抽取来自模型 —— `entity.origin` / `fact.quote` 里能看到 `llm` 产出。

用法：

```bash
# 离线自检（不需要模型）
python scripts/check_llm_judge.py --mock --limit 40

# 接自己的 4B
python scripts/check_llm_judge.py --endpoint http://localhost:8000/v1 --model qwen3-4b --limit 40
```
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PDF = r"D:\WechatHistory\xwechat_files\wxid_9i084z6u5z0322_db89\msg\file\2026-10\克苏鲁的呼唤第七版规则书1810.pdf"

_ITEM_RE = re.compile(r"### \[(\d+)\] 标题：(.*?)\n```(?:json)?\n(.*?)\n```", re.S)


class Mock:
    mode = "ok"      # ok / fabricate
    task = "classify"


def _kind_of(text: str) -> str:
    """mock 的判别逻辑：故意写得很粗 —— 它只要能过 spec 校验就够，不需要真的准。"""
    t = text
    if re.search(r"STR|CON|SIZ|DEX|INT|POW|APP|EDU", t) and ("平均" in t or "掷骰" in t):
        return "npc"
    if re.search(r"[$￥]\s*\d|\d+\s*美分", t):
        return "tool"
    if re.search(r"检定|掷骰|d100|D100|轮|伤害", t):
        return "rule"
    if re.search(r"属性|技能点|职业|角色卡", t):
        return "pc"
    return "lore"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        prompt = "".join(str(m.get("content") or "") for m in (req.get("messages") or []))
        items = _ITEM_RE.findall(prompt)

        out = []
        for idx, title, body in items:
            body = body.strip()
            if "/ 待抽条目" in prompt or "要抽什么" in prompt:
                ents = _fake_entities(title, body)
                out.append({"id": idx, "entities": ents})
                continue
            k = _kind_of(body)
            ev = body[:10]
            if Mock.mode == "fabricate":
                ev = "这句话在原文里根本不存在ZZZ"
            out.append({"id": idx, "kind": k, "subtype": "测试", "evidence": ev, "confidence": 0.72})

        payload = {"items": out}
        resp = {
            "id": "mock", "object": "chat.completion", "created": 0,
            "model": req.get("model") or "mock",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": len(prompt) // 2, "completion_tokens": 60,
                      "total_tokens": len(prompt) // 2 + 60},
        }
        raw = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _fake_entities(title: str, body: str) -> list[dict]:
    """mock 的抽取：把"标题"当实体名，把正文里的"标签：数值"当字段。"""
    name = re.sub(r"^\d+(\.\d+)*\s*", "", title).strip()
    if len(name) < 2 or len(name) > 30:
        return []
    fields = []
    for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z]{2,6})[：:]\s*([^\n，。；]{1,20})", body):
        label, value = m.group(1), m.group(2).strip()
        if label in ("属性", "注意", "说明"):
            continue
        fields.append({"label": label, "value": value, "quote": m.group(0)})
        if len(fields) >= 4:
            break
    if not fields and "$" not in body:
        return []
    etype = "monster" if _kind_of(body) == "npc" else ("item" if _kind_of(body) == "tool" else "lore")
    return [{"name": name, "type": etype, "subtype": "测试", "fields": fields}]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM 判别链路自检")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--fabricate", action="store_true", help="让 mock 编造证据，验证校验会拦")
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--model", default="qwen3-4b")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--out", default="data/kb/_judge")
    ap.add_argument("--skip-extract", action="store_true", help="只跑判别，不跑实体抽取")
    ap.add_argument("--workers", type=int, default=1,
                    help="并发数。本地引擎同一权重不支持并发 generate，默认 1（串行）")
    args = ap.parse_args(argv)

    if args.mock:
        Mock.mode = "fabricate" if args.fabricate else "ok"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{srv.server_port}/v1"
    elif args.endpoint:
        endpoint = args.endpoint.rstrip("/")
    else:
        print("给 --mock 或 --endpoint")
        return 2

    os.environ["RPGBAR_LLM"] = f"{args.model}@{endpoint}|sk-local"
    from compiler.graph import run_compile

    shutil.rmtree(args.out, ignore_errors=True)
    st = run_compile(
        PDF, args.out, limit_pages=args.limit, with_vec=False,
        options={"use_llm": True, "llm_mode": "all", "llm_batch": 8,
                 "llm_workers": args.workers,
                 "llm_extract": not args.skip_extract},
    )
    print()
    for t in st.get("trace") or []:
        if t["node"] in ("classify", "llm_extract", "report"):
            print(f"  [{t['node']}] {t['note']}")

    con = sqlite3.connect(os.path.join(args.out, "kb.sqlite"))
    con.row_factory = sqlite3.Row
    by_llm = con.execute(
        "SELECT COUNT(*) n FROM block WHERE json_extract(meta,'$.verdict.by')='llm'"
    ).fetchone()["n"]
    by_rule = con.execute(
        "SELECT COUNT(*) n FROM block WHERE json_extract(meta,'$.verdict.by')='rule'"
    ).fetchone()["n"]
    tot = con.execute("SELECT COUNT(*) n FROM block").fetchone()["n"]
    marked = con.execute(
        "SELECT COUNT(*) n FROM block WHERE json_extract(meta,'$.verdict.llm_rejected') IS NOT NULL"
    ).fetchone()["n"]
    llm_fields = con.execute("SELECT COUNT(*) n FROM field WHERE origin='llm'").fetchone()["n"]
    llm_ents = con.execute("SELECT COUNT(*) n FROM entity WHERE etype IN ('lore','item','monster')").fetchone()["n"]

    print()
    print(f"块 {tot}｜判源：llm {by_llm} / rule {by_rule}（其余为切分阶段定类）")
    print(f"被判废后回落规则的块：{marked}")
    print(f"字段字典里 origin=llm 的字段：{llm_fields}")
    print(f"库中 lore/item/monster 实体：{llm_ents}")
    ok = (by_llm > 0) if not args.fabricate else (by_llm == 0 and marked > 0)
    print()
    if args.fabricate:
        print("结论：" + ("✓ 校验拦住了编造证据，全部回落规则，库未被污染"
                         if ok else "✗ 编造的判定进了库，护栏失效"))
    else:
        print("结论：" + ("✓ LLM 已接管判别（by=llm），且规则只做兜底"
                         if ok else "✗ 判别没有走到模型"))
    con.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
