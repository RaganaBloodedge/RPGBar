"""把你的 `rpg_engine.dll` / `rpg_engine_cuda.dll` 包成一个 OpenAI 兼容端点。

为什么走这条路，而不是装 llama.cpp / ollama / vLLM：

- 引擎是你自己写的，权重格式（GGUF Q4_K_M）与 chat template（Qwen3）它已经处理好了
  （`engine.generate` 直接吃 `messages`），再套一层别的框架等于把已经解决的问题重做一遍；
- LangChain 只要一个 `/v1/chat/completions` 就能用，协议本身很薄。

于是整条链路变成：

    LangChain ChatOpenAI → http://127.0.0.1:PORT/v1 → rpg_engine → Qwen3-4B-Q4_K_M.gguf

用法：

    python scripts/rpg_engine_server.py --model models/Qwen3-4B-Q4_K_M.gguf --port 8080
    # 然后
    RPGBAR_LLM="qwen3-4b@http://127.0.0.1:8080/v1|sk-local" \
      python -m compiler.cli graph --pdf 书.pdf --out data/kb/x --use-llm

两个必须注意的引擎限制（都是实测出来的，写在 `server/llm.py` 的注释里）：

1. **同一份权重不支持并发 generate** —— 两个线程同时跑会让进程无声消失。所以这里用
   一把全局锁串行化，而不是"多开几个线程提高吞吐"。
2. **会话带 KV cache** —— 每次请求先 `reset_session`，保证请求之间互不污染。
   （判别任务每次都是完整的 messages，不需要跨请求记忆。）
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine import find_lib, open_lib  # noqa: E402

_LOCK = threading.Lock()  # 引擎硬限制：同权重同时只能跑一个 generate
_STATE: dict = {"lib": None, "model": None, "session": None, "model_path": "",
                "max_seq": 8192, "stats": []}


def load(model_path: str, backend: str = "gpu", max_pos: int = 8192, threads: int = 0) -> None:
    t0 = time.time()
    lib_path = find_lib("", backend=backend)
    if lib_path is None:
        raise SystemExit(f"找不到 {backend} 后端库（models/ 下应有 rpg_engine*.dll）")
    lib = open_lib(lib_path)
    model = lib.model_for(model_path, threads=threads, max_pos=max_pos)
    _STATE.update(lib=lib, model=model, model_path=model_path, max_seq=max_pos)
    info = lib.model_info(model)
    print(f"[engine] {Path(lib_path).name} 加载 {Path(model_path).name} 用时 {time.time()-t0:.1f}s")
    print(f"[engine] {json.dumps({k: v for k, v in info.items() if not isinstance(v, (list, dict))}, ensure_ascii=False)}")


def complete(messages: list[dict], max_tokens: int, temperature: float) -> dict:
    lib, model = _STATE["lib"], _STATE["model"]
    if lib is None or model is None:
        raise RuntimeError("模型未加载")
    with _LOCK:
        sess = _STATE.get("session")
        if sess is None:
            # max_seq 必须显式给：引擎把 0 当成非法值（"max_seq=0 超出模型位置范围"），
            # 所以别用 0 表示"默认"。这里取启动时声明的上下文长度。
            sess = lib.new_session(model, max_seq=_STATE["max_seq"], max_tokens=max_tokens)
            _STATE["session"] = sess
        else:
            lib.reset_session(sess)  # 会话带 KV cache，请求之间必须清
        t0 = time.time()
        r = lib.generate(sess, {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "enable_thinking": False,  # 判别任务不需要思维链：更短、更稳、更快
        })
        r["_seconds"] = round(time.time() - t0, 2)
    _STATE["stats"].append(r["_seconds"])
    return r


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, obj: dict, code: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send({"object": "list", "data": [
                {"id": Path(_STATE["model_path"]).stem, "object": "model", "owned_by": "local"}
            ]})
        if "/stats" in self.path:
            s = _STATE["stats"]
            return self._send({"calls": len(s), "avg_seconds": round(sum(s) / len(s), 2) if s else 0,
                               "last_seconds": s[-1] if s else None})
        return self._send({"ok": True, "model": _STATE["model_path"]})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send({"error": {"message": "bad json"}}, 400)

        messages = req.get("messages") or []
        max_tokens = int(req.get("max_tokens") or req.get("max_completion_tokens") or 512)
        temperature = float(req.get("temperature") if req.get("temperature") is not None else 0.0)
        try:
            r = complete(messages, max_tokens, temperature)
        except Exception as e:  # noqa: BLE001  引擎异常要回给客户端，不能把服务弄挂
            return self._send({"error": {"message": f"{type(e).__name__}: {e}"}}, 500)

        text = r.get("text") or ""
        usage = r.get("usage") or {}
        self._send({
            "id": f"chatcmpl-{int(time.time()*1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model") or Path(_STATE["model_path"]).stem,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens") or usage.get("prompt") or 0,
                "completion_tokens": usage.get("completion_tokens") or usage.get("completion") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
            },
        })


def main() -> int:
    ap = argparse.ArgumentParser(description="rpg_engine → OpenAI 兼容端点")
    ap.add_argument("--model", default="models/Qwen3-4B-Q4_K_M.gguf")
    ap.add_argument("--backend", default="gpu", choices=["gpu", "cpu"])
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--max-pos", type=int, default=8192, help="上下文长度：判别提示词约 3-5k token")
    ap.add_argument("--threads", type=int, default=0)
    args = ap.parse_args()

    load(args.model, args.backend, args.max_pos, args.threads)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(f"[engine] 监听 http://127.0.0.1:{args.port}/v1  （Ctrl-C 退出）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        from server.engine import unload_all

        unload_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
