"""Ollama agent client.

Runs an agent loop: sends a message, executes any tool calls the model
requests, feeds results back, repeats until the model stops calling tools
and returns a final reply.
"""

from __future__ import annotations

import json

import ollama

from . import tools as _tools
from .. import config


def check_ollama() -> None:
    """Raise RuntimeError if Ollama is unreachable or the model is not pulled."""
    try:
        client = ollama.Client()
        models = [m.model for m in client.list().models]
    except Exception as exc:
        raise RuntimeError(
            f"Ollama is not running or unreachable: {exc}\n"
            "Start it with: ollama serve"
        ) from exc

    if config.OLLAMA_MODEL not in models:
        raise RuntimeError(
            f"Model '{config.OLLAMA_MODEL}' is not pulled.\n"
            f"Pull it with: ollama pull {config.OLLAMA_MODEL}\n"
            f"Available models: {', '.join(models) or '(none)'}"
        )


class AIClient:
    """Stateless agent. Each call() runs one full agent turn with tool use."""

    def __init__(self) -> None:
        self._client = ollama.Client()
        self._model  = config.OLLAMA_MODEL

    def call(self, system: str, message: str) -> str:
        """Send a message with a system prompt; execute tools until done.

        Returns the model's final text reply.
        """
        messages: list[dict] = [
            {"role": "system",  "content": system},
            {"role": "user",    "content": message},
        ]

        while True:
            response = self._client.chat(
                model=self._model,
                messages=messages,
                tools=_tools.TOOLS,
                think=False,
            )
            msg = response.message

            if not msg.tool_calls:
                return (msg.content or "").strip()

            # Append assistant turn and execute each tool call
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": msg.tool_calls,
            })

            for tc in msg.tool_calls:
                name   = tc.function.name
                args   = dict(tc.function.arguments) if tc.function.arguments else {}
                result = _tools.dispatch(name, args)
                messages.append({
                    "role":    "tool",
                    "content": json.dumps(result, default=str),
                })
