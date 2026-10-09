"""LLM Provider 抽象：OpenAI 兼容接口，云 API 与本地模型共用同一套。

两条通道都走 OpenAI 兼容协议，只是 `base_url` 不同：
- 云端 API：https://api.deepseek.com/v1、https://api.openai.com/v1、…（需要 api_key）
- 本地模型：llama.cpp server（`llama-server`，默认 :8080）、Ollama（:11434/v1）、LM Studio（:1234/v1）

除了最基础的单轮 `chat()`，还提供标准 Agent 所需的 `chat_with_tools()`：
把工具（JSON Schema）发给模型，模型返回 `tool_calls`，由调用方执行后把结果回灌。
"""


class ModelError(Exception):
    """模型调用失败（网络、鉴权、参数等）。"""


class ToolCallingUnsupported(ModelError):
    """该模型/服务不支持工具调用——调用方应降级为单轮 JSON 协议。"""


class LLMProvider:
    name = "base"
    supports_tools = True

    async def chat(self, messages, json_mode: bool = False) -> str:
        raise NotImplementedError

    async def chat_with_tools(self, messages, tools) -> dict:
        """返回 {"content": str, "tool_calls": [...]}。"""
        raise NotImplementedError

    async def close(self):
        pass


def _looks_like_no_tools(status: int, body: str) -> bool:
    if status not in (400, 404, 422, 500):
        return False
    low = (body or "").lower()
    return any(k in low for k in ("tool", "function", "tool_choice", "tools"))


class OpenAICompatProvider(LLMProvider):
    name = "openai-compat"

    def __init__(self, base_url, api_key, model, temperature=0.8, timeout=120.0, label="cloud"):
        import httpx

        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.temperature = temperature
        self.label = label  # "cloud" / "local"
        self.name = f"openai-compat[{label}]"
        self._httpx = httpx
        self._client = httpx.AsyncClient(timeout=timeout)

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def chat(self, messages, json_mode: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = await self._client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
        )
        if resp.status_code >= 400:
            raise ModelError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return (data["choices"][0]["message"].get("content") or "")

    async def chat_with_tools(self, messages, tools) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
        resp = await self._client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
        )
        if resp.status_code >= 400:
            body = resp.text or ""
            if tools and _looks_like_no_tools(resp.status_code, body):
                raise ToolCallingUnsupported(f"HTTP {resp.status_code}: {body[:200]}")
            raise ModelError(f"HTTP {resp.status_code}: {body[:200]}")
        data = resp.json()
        msg = data["choices"][0]["message"]
        return {"content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}

    async def close(self):
        await self._client.aclose()


def build_provider(slot) -> OpenAICompatProvider | None:
    """由一个「模型槽位」配置构建 Provider；返回 None 表示走脚本化兜底。

    槽位字段：kind（cloud/local/off）+ base_url + api_key + model + temperature。
    - kind=off（默认，即"留空"）：不接模型，全部走脚本化兜底。
    - kind=cloud：必须有 api_key。
    - kind=local：不要求 api_key（本地服务一般不需要）。
    """
    if not isinstance(slot, dict):
        return None
    kind = (slot.get("kind") or "off").lower()
    if kind in ("off", "none", "scripted", ""):
        return None
    base_url = (slot.get("base_url") or "").strip()
    model = (slot.get("model") or "").strip()
    api_key = (slot.get("api_key") or "").strip()
    if kind == "cloud" and not api_key:
        return None
    if not base_url or not model:
        return None
    return OpenAICompatProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=float(slot.get("temperature", 0.8)),
        timeout=float(slot.get("timeout", 120.0)),
        label="local" if kind == "local" else "cloud",
    )
