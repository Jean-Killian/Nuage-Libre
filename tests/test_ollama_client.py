"""Unit tests for OllamaClient's parsing of Ollama's streaming protocol.

httpx.MockTransport replaces the network: we hand-craft the exact bytes
Ollama would send and check how the client interprets them. No database,
no server, no GPU needed.
"""

import json

import httpx
import pytest

from app.services import ollama_client as module
from app.services.ollama_client import OllamaClient, OllamaError


def ndjson(*objects: dict) -> bytes:
    return "".join(json.dumps(o) + "\n" for o in objects).encode()


@pytest.fixture
def mock_ollama(monkeypatch):
    """Make every httpx.AsyncClient created by the module use a fake transport."""

    def install(handler):
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(module.httpx, "AsyncClient", factory)

    return install


async def collect(client: OllamaClient):
    return [c async for c in client.stream_chat("m", [{"role": "user", "content": "hi"}], 2048)]


async def test_parses_thinking_content_and_stats(mock_ollama):
    """T-OLL-01: thinking, content and final statistics are extracted (ns -> ms)."""
    body = ndjson(
        {"message": {"role": "assistant", "thinking": "hmm"}, "done": False},
        {"message": {"role": "assistant", "content": "Hello"}, "done": False},
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "eval_count": 7,
            "total_duration": 2_500_000_000,
        },
    )
    mock_ollama(lambda request: httpx.Response(200, content=body))

    chunks = await collect(OllamaClient("http://ollama"))

    assert [c.thinking for c in chunks] == ["hmm", "", ""]
    assert [c.content for c in chunks] == ["", "Hello", ""]
    assert chunks[-1].done and chunks[-1].token_count == 7 and chunks[-1].total_ms == 2500


async def test_sends_model_and_context_size(mock_ollama):
    """T-OLL-02: the request carries the model name and num_ctx (VRAM control)."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=ndjson({"done": True}))

    mock_ollama(handler)
    await collect(OllamaClient("http://ollama"))

    assert seen["model"] == "m"
    assert seen["options"] == {"num_ctx": 2048}
    assert seen["stream"] is True


async def test_http_error_is_reported(mock_ollama):
    """T-OLL-03: an HTTP error from Ollama (e.g. unknown model) becomes an OllamaError."""
    mock_ollama(lambda request: httpx.Response(404, json={"error": "model 'm' not found"}))

    with pytest.raises(OllamaError, match="404.*model 'm' not found"):
        await collect(OllamaClient("http://ollama"))


async def test_error_in_the_middle_of_the_stream(mock_ollama):
    """T-OLL-04: an error line during generation (e.g. GPU out of memory) is raised."""
    body = ndjson({"message": {"content": "Hel"}, "done": False}, {"error": "out of memory"})
    mock_ollama(lambda request: httpx.Response(200, content=body))

    with pytest.raises(OllamaError, match="out of memory"):
        await collect(OllamaClient("http://ollama"))


async def test_truncated_stream_is_an_error(mock_ollama):
    """T-OLL-05: a stream that ends without "done" is not silently accepted."""
    mock_ollama(lambda request: httpx.Response(200, content=ndjson({"message": {"content": "Hel"}})))

    with pytest.raises(OllamaError, match="before the answer was complete"):
        await collect(OllamaClient("http://ollama"))


async def test_connection_refused(mock_ollama):
    """T-OLL-06: Ollama not running gives a clear message."""

    def handler(request):
        raise httpx.ConnectError("refused")

    mock_ollama(handler)

    with pytest.raises(OllamaError, match="Is the container running"):
        await collect(OllamaClient("http://ollama"))


async def test_malformed_lines_are_skipped(mock_ollama):
    """T-OLL-07: a corrupted line does not abort the whole answer."""
    body = b"not json\n" + ndjson({"message": {"content": "ok"}}, {"done": True})
    mock_ollama(lambda request: httpx.Response(200, content=body))

    chunks = await collect(OllamaClient("http://ollama"))

    assert "".join(c.content for c in chunks) == "ok"
