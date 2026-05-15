import os
import anthropic
from typing import AsyncIterator, Optional


class BaseAgent:
    model = "claude-sonnet-4-6"
    max_tokens = 2048

    def __init__(self, system_prompt: str, agent_type: str):
        self.client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.system_prompt = system_prompt
        self.agent_type = agent_type

    def _system_with_cache(self) -> list:
        return [{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}]

    async def run(self, prompt: str, context: Optional[str] = None) -> str:
        messages = []
        if context:
            messages.append({"role": "user", "content": [
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
            ]})
            messages.append({"role": "assistant", "content": "Contexto recebido. Aguardando instrução."})
        messages.append({"role": "user", "content": prompt})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._system_with_cache(),
            messages=messages,
            betas=["prompt-caching-2024-07-31"],
        )
        return response.content[0].text

    async def stream(self, prompt: str, context: Optional[str] = None) -> AsyncIterator[str]:
        messages = []
        if context:
            messages.append({"role": "user", "content": [
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
            ]})
            messages.append({"role": "assistant", "content": "Contexto recebido. Aguardando instrução."})
        messages.append({"role": "user", "content": prompt})

        async with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._system_with_cache(),
            messages=messages,
            betas=["prompt-caching-2024-07-31"],
        ) as s:
            async for text in s.text_stream:
                yield text
