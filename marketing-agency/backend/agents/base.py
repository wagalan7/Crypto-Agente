import os
from typing import AsyncIterator, Optional
from groq import AsyncGroq


class BaseAgent:
    model = "llama-3.3-70b-versatile"
    max_tokens = 2048

    def __init__(self, system_prompt: str, agent_type: str):
        self.client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
        self.system_prompt = system_prompt
        self.agent_type = agent_type

    async def run(self, prompt: str, context: Optional[str] = None) -> str:
        messages = [{"role": "system", "content": self.system_prompt}]
        if context:
            messages.append({"role": "user", "content": context})
            messages.append({"role": "assistant", "content": "Contexto recebido. Aguardando instrução."})
        messages.append({"role": "user", "content": prompt})

        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
        )
        return response.choices[0].message.content

    async def stream(self, prompt: str, context: Optional[str] = None) -> AsyncIterator[str]:
        messages = [{"role": "system", "content": self.system_prompt}]
        if context:
            messages.append({"role": "user", "content": context})
            messages.append({"role": "assistant", "content": "Contexto recebido. Aguardando instrução."})
        messages.append({"role": "user", "content": prompt})

        stream = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
