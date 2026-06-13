"""Thin Anthropic wrapper: cached, JSON-only responses."""

from __future__ import annotations

import json

import anthropic

from .cache import Cache


class LLM:
    def __init__(self, cache: Cache, max_tokens: int = 1024):
        self._cache = cache
        self._max_tokens = max_tokens
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        # Lazy init lets offline stages run without a key.
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def json(self, model: str, system: str, user: str, schema: dict) -> dict:
        key = json.dumps(
            {"model": model, "system": system, "user": user, "schema": schema},
            sort_keys=True,
            ensure_ascii=False,
        )
        cached = self._cache.get("llm", key)
        if cached is not None:
            return cached
        result = self._call(model, system, user, schema)
        self._cache.set("llm", key, result)
        return result

    def _call(self, model: str, system: str, user: str, schema: dict) -> dict:
        last_error: Exception | None = None
        for _ in range(2):
            response = self.client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                return json.loads(text)
            except json.JSONDecodeError as error:
                last_error = error
        raise ValueError(f"LLM returned invalid JSON after one retry: {last_error}")
