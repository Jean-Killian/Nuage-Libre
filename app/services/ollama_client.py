"""Client for the local Ollama inference server.

This module is the only place that knows Ollama's HTTP protocol. The rest
of the application works with simple Python objects (``ChatChunk``) and a
single exception type (``OllamaError``). If we later switch engine (e.g. to
llama.cpp server or vLLM), only this file needs to change.

Ollama streaming protocol (``POST /api/chat`` with ``"stream": true``):
the response is NDJSON, i.e. one JSON object per line, for example:

    {"message": {"role": "assistant", "thinking": "The user asks..."}, "done": false}
    {"message": {"role": "assistant", "content": "Hello"}, "done": false}
    {"message": {"role": "assistant", "content": "!"}, "done": false}
    {"done": true, "eval_count": 42, "total_duration": 1234567890, ...}

Durations are given in nanoseconds.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


class OllamaError(Exception):
    """Any failure talking to Ollama, with a message safe to show the user."""


@dataclass(frozen=True)
class ChatChunk:
    """One piece of a streamed answer.

    Exactly one of ``thinking`` / ``content`` is usually set. The last chunk
    has ``done=True`` and carries the generation statistics.
    """

    thinking: str = ""
    content: str = ""
    done: bool = False
    token_count: int | None = None  # number of generated tokens (eval_count)
    total_ms: int | None = None  # total generation time in milliseconds


@dataclass(frozen=True)
class ModelInfo:
    """One model installed in Ollama, as reported by ``GET /api/tags``."""

    name: str  # full tag, e.g. "qwen3:4b" or "hf.co/user/repo:Q4_K_M"
    size_bytes: int  # size of the weights on disk (~ VRAM needed, before the KV cache)
    family: str | None
    parameter_size: str | None  # e.g. "4.0B"
    quantization: str | None  # e.g. "Q4_K_M"
    # What the model can do: "completion" (chat), "thinking" (reasoning),
    # "vision" (images), "tools", "embedding"...
    capabilities: tuple[str, ...]


class OllamaClient:
    def __init__(self, base_url: str, read_timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        # connect: fail fast (5 s) if Ollama is not running.
        # read: a big model can take a while before the first token
        #       (loading weights into VRAM), so we are more patient here.
        self.timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=30.0, pool=5.0)

    async def version(self) -> str:
        """Return the Ollama version; raises OllamaError if unreachable."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.base_url}/api/version")
                response.raise_for_status()
                return response.json().get("version", "unknown")
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama unreachable at {self.base_url}") from exc

    async def list_models(self) -> list[ModelInfo]:
        """Return every model installed in Ollama.

        Recent Ollama versions include ``capabilities`` in this listing. If an
        older version omits it, we assume a plain chat model ("completion").
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OllamaError(f"Cannot list models from Ollama at {self.base_url}") from exc

        models = []
        for item in data.get("models", []):
            details = item.get("details") or {}
            models.append(
                ModelInfo(
                    name=item["name"],
                    size_bytes=item.get("size", 0),
                    family=details.get("family"),
                    parameter_size=details.get("parameter_size"),
                    quantization=details.get("quantization_level"),
                    capabilities=tuple(item.get("capabilities") or ("completion",)),
                )
            )
        return models

    async def stream_chat(
        self, model: str, messages: list[dict[str, str]], num_ctx: int
    ) -> AsyncIterator[ChatChunk]:
        """Send the conversation to the model and yield the answer piece by piece.

        ``messages`` is the list of ``{"role": ..., "content": ...}`` dicts in
        chronological order. The model has no memory of its own: the full
        context must be sent with every request.
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            # num_ctx sets the context window, and therefore the VRAM
            # reserved for the KV cache. It is set explicitly to keep memory
            # usage predictable and avoid out-of-memory crashes.
            "options": {"num_ctx": num_ctx},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                    if response.status_code != 200:
                        # Ollama answers errors as {"error": "..."},
                        # e.g. 404 "model 'xyz' not found".
                        body = (await response.aread()).decode(errors="ignore")
                        try:
                            detail = json.loads(body).get("error", body)
                        except json.JSONDecodeError:
                            detail = body
                        raise OllamaError(f"Ollama error ({response.status_code}): {detail}")

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # ignore a malformed line rather than abort
                        # Errors can also happen mid-stream (e.g. GPU OOM).
                        if "error" in data:
                            raise OllamaError(f"Ollama error: {data['error']}")

                        message = data.get("message", {})
                        if data.get("done"):
                            total_ns = data.get("total_duration")
                            yield ChatChunk(
                                content=message.get("content", ""),
                                done=True,
                                token_count=data.get("eval_count"),
                                total_ms=total_ns // 1_000_000 if total_ns else None,
                            )
                            return
                        yield ChatChunk(
                            thinking=message.get("thinking", ""),
                            content=message.get("content", ""),
                        )
        except httpx.ConnectError as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}. Is the container running?") from exc
        except httpx.TimeoutException as exc:
            raise OllamaError("Ollama took too long to answer (timeout).") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Connection to Ollama failed: {exc}") from exc
        # The stream ended without a final "done" message: the server crashed
        # or the connection was cut. Report it instead of silently truncating.
        raise OllamaError("Ollama closed the stream before the answer was complete.")
