"""LLM Provider 抽象：OpenAI 兼容接口，云 API 与本地 llama.cpp 共用同一套。"""
import httpx


class LLMProvider:
    name = "base"

    async def chat(self, messages, json_mode: bool = False) -> str:
        raise NotImplementedError

    async def close(self):
        pass


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    def __init__(self, base_url, api_key, model, temperature=0.8):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self._client = httpx.AsyncClient(timeout=90.0)

    async def chat(self, messages, json_mode: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"
        resp = await self._client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def close(self):
        await self._client.aclose()


def build_provider(cfg) -> LLMProvider | None:
    """返回 provider，或 None 表示走脚本化兜底。"""
    llm = cfg.get("llm", {})
    if llm.get("provider") == "openai" and llm.get("api_key"):
        return OpenAICompatProvider(
            llm["base_url"], llm["api_key"], llm["model"], llm.get("temperature", 0.8)
        )
    return None
