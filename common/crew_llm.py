"""CrewLLMAdapter: bridges CrewAI to common.llm_client.LLMClient."""

from __future__ import annotations

import asyncio
from typing import Any

from crewai import BaseLLM
from pydantic import Field

from common.llm_client import LLMClient, Message


class CrewLLMAdapter(BaseLLM):
    """CrewAI BaseLLM adapter delegating calls to LLMClient."""

    model: str = Field(default="gemini-2.5-flash")
    llm_client: Any = Field(default=None, exclude=True)

    def __init__(self, llm_client: LLMClient | None = None, model: str = "gemini-2.5-flash", **kwargs: Any) -> None:
        super().__init__(model=model, **kwargs)
        self.llm_client = llm_client or LLMClient(model_name=model)

    def call(
        self,
        messages: str | list[Any],
        tools: list[Any] | None = None,
        callbacks: list[Any] | None = None,
        available_functions: dict[str, Any] | None = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
    ) -> str | Any:
        """Synchronously execute chat using LLMClient."""
        # Convert messages to uniform format
        converted_messages: list[Message] = []
        if isinstance(messages, str):
            converted_messages = [Message(role="user", content=messages)]
        else:
            for m in messages:
                if isinstance(m, Message):
                    converted_messages.append(m)
                elif hasattr(m, "role") and hasattr(m, "content"):
                    converted_messages.append(Message(role=str(m.role), content=str(m.content)))
                elif isinstance(m, dict):
                    converted_messages.append(
                        Message(role=str(m.get("role", "user")), content=str(m.get("content", "")))
                    )
                else:
                    converted_messages.append(Message(role="user", content=str(m)))

        # Run async chat in event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # If called from within an existing event loop, run in a separate thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    self.llm_client.chat(
                        messages=converted_messages,
                        tools=tools,
                        response_schema=response_model,
                    ),
                ).result()
        else:
            result = asyncio.run(
                self.llm_client.chat(
                    messages=converted_messages,
                    tools=tools,
                    response_schema=response_model,
                )
            )

        if hasattr(result, "content"):
            return result.content or ""
        return result
