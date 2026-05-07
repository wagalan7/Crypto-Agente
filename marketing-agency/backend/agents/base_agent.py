import anthropic
import os
from typing import AsyncIterator


class BaseAgent:
    def __init__(self, system_prompt: str):
        self.client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.system_prompt = system_prompt
        self.model = "claude-sonnet-4-6"

    async def run(self, user_message: str) -> str:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return message.content[0].text

    async def stream(self, user_message: str) -> AsyncIterator[str]:
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=1024,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            async for text in stream.text_stream:
                yield text
