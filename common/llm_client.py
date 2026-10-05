"""Provider-neutral entry point for the project's Gemini Flash chat model."""

from __future__ import annotations

import os
from typing import Any, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool


class LLMClient:
	"""Create or wrap a chat model; accepting a model keeps tests offline."""

	def __init__(
		self,
		model: BaseChatModel | None = None,
		*,
		api_key: str | None = None,
		model_name: str | None = None,
		timeout_seconds: float = 30.0,
	) -> None:
		if timeout_seconds <= 0:
			raise ValueError("timeout_seconds must be positive")
		if model is None:
			configured_key = api_key or os.getenv("GEMINI_API_KEY")
			if not configured_key:
				raise ValueError("GEMINI_API_KEY must be configured")
			from langchain_google_genai import ChatGoogleGenerativeAI

			model = ChatGoogleGenerativeAI(
				model=model_name or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
				google_api_key=configured_key,
				temperature=0,
				max_retries=0,
				timeout=timeout_seconds,
			)
		self.model = model

	def bind_tools(self, tools: Sequence[BaseTool]) -> Any:
		"""Return the configured model with the supplied read-only tools bound."""
		return self.model.bind_tools(list(tools))