"""
llm_client.py

A tiny provider-agnostic wrapper around whichever LLM API you're using.
patch_generator.py only ever calls `.complete(system, user) -> str`, so
swapping providers later (e.g. Gemini now -> Claude later) means
changing ONE line in orchestrator.py, nothing else in the pipeline.

Why this indirection exists at all: the interesting logic in this
project is the prompt design, the diff parsing, the retry loop — none
of that should care which LLM vendor is underneath. Tying the whole
pipeline directly to one SDK would make that swap (and testing/mocking)
much more annoying later.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


class LLMClient(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Send a system + user prompt, return the raw text response."""
        raise NotImplementedError


class GeminiClient(LLMClient):
    """
    Uses Google's Gemini API via the current `google-genai` SDK
    (the older `google-generativeai` package is deprecated/unmaintained
    as of 2026). Install with:
        pip install google-genai

    Get a key at https://aistudio.google.com/apikey (free tier available).
    """

    def __init__(self, api_key: str | None = None, model: str = "gemini-2.5-flash"):
        from google import genai

        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "No Gemini API key found. Pass api_key= or set GEMINI_API_KEY env var."
            )
        self._client = genai.Client(api_key=self.api_key)
        self._model_name = model

    def complete(self, system: str, user: str) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model_name,
            contents=user,
            config=types.GenerateContentConfig(system_instruction=system),
        )
        return response.text


class AnthropicClient(LLMClient):
    """
    Uses the Anthropic API. Install with:
        pip install anthropic

    Swap to this later by changing ONE line in orchestrator.py:
        llm_client = AnthropicClient(api_key=..., model="claude-sonnet-4-6")
    """

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-6"):
        import anthropic

        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        self.model = model

    def complete(self, system: str, user: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text


class MockClient(LLMClient):
    """
    Returns a fixed response — useful for testing orchestrator.py's
    control flow (retry loop, scope validation, etc.) without spending
    any API credits at all.
    """

    def __init__(self, fixed_response: str):
        self.fixed_response = fixed_response

    def complete(self, system: str, user: str) -> str:
        return self.fixed_response
