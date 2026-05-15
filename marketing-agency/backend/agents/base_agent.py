import os
from typing import AsyncIterator
from groq import AsyncGroq


class BaseAgent:
    model = "llama-3.3-70b-versatile"

    def __init__(self, system_prompt: str):
        self.client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
        self.system_prompt = system_prompt

    async def run(self, user_message: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        return response.choices[0].message.content

    async def stream(self, user_message: str) -> AsyncIterator[str]:
        stream = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
