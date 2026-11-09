"""Tests for model selection: GET /api/models and the "model" field of POST /api/chat."""

from app.config import get_settings
from app.services.ollama_client import OllamaError
from tests.conftest import parse_events

DEFAULT = get_settings().model


async def test_models_lists_chat_models_smallest_first(client):
    """T-MOD-01: the menu lists chat models, smallest first, and flags the default."""
    response = await client.get("/api/models")

    assert response.status_code == 200
    models = response.json()
    assert [m["name"] for m in models] == ["qwen3:4b", DEFAULT]
    assert [m["is_default"] for m in models] == [False, True]
    assert models[0]["thinking"] is True and models[0]["parameter_size"] == "4.0B"


async def test_embedding_models_are_hidden(client):
    """T-MOD-02: embedding-only models cannot hold a conversation and are not offered."""
    names = [m["name"] for m in (await client.get("/api/models")).json()]

    assert "nomic-embed-text:latest" not in names


async def test_models_unavailable_when_ollama_down(client, fake_ollama):
    """T-MOD-03: the menu endpoint reports 503 if Ollama cannot be reached."""
    fake_ollama.error = OllamaError("Cannot reach Ollama")

    response = await client.get("/api/models")

    assert response.status_code == 503


async def test_chat_uses_requested_model(client, fake_ollama):
    """T-MOD-04: the model chosen in the menu is the one called and stored."""
    events = parse_events(await client.post("/api/chat", json={"message": "Hi", "model": "qwen3:4b"}))

    assert fake_ollama.calls[-1]["model"] == "qwen3:4b"
    assert events[0]["model"] == "qwen3:4b" and events[-1]["model"] == "qwen3:4b"
    detail = (await client.get(f"/api/conversations/{events[0]['id']}")).json()
    assert detail["model"] == "qwen3:4b"
    assert [m["model"] for m in detail["messages"]] == [None, "qwen3:4b"]


async def test_new_chat_without_model_uses_default(client, fake_ollama):
    """T-MOD-05: no model in the request -> the configured default model."""
    await client.post("/api/chat", json={"message": "Hi"})

    assert fake_ollama.calls[-1]["model"] == DEFAULT


async def test_switch_model_mid_conversation(client, fake_ollama):
    """T-MOD-06: each answer records its own model; the conversation remembers the latest."""
    conversation_id = parse_events(await client.post("/api/chat", json={"message": "one"}))[0]["id"]

    await client.post(
        "/api/chat", json={"conversation_id": conversation_id, "message": "two", "model": "qwen3:4b"}
    )
    # No model sent: continue with the model of the latest answer.
    await client.post("/api/chat", json={"conversation_id": conversation_id, "message": "three"})

    assert [c["model"] for c in fake_ollama.calls] == [DEFAULT, "qwen3:4b", "qwen3:4b"]
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert detail["model"] == "qwen3:4b"
    answers = [m["model"] for m in detail["messages"] if m["role"] == "assistant"]
    assert answers == [DEFAULT, "qwen3:4b", "qwen3:4b"]


async def test_unknown_model_is_rejected(client, fake_ollama):
    """T-MOD-07 / T-SEC-02: a model that is not installed is refused (allow-list)."""
    response = await client.post("/api/chat", json={"message": "Hi", "model": "evil-model:latest"})

    assert response.status_code == 422
    assert response.json() == {"detail": "This model is not installed."}
    assert fake_ollama.calls == []


async def test_embedding_model_cannot_be_used_for_chat(client, fake_ollama):
    """T-MOD-08: an installed but embedding-only model is refused too."""
    response = await client.post("/api/chat", json={"message": "Hi", "model": "nomic-embed-text:latest"})

    assert response.status_code == 422
    assert fake_ollama.calls == []
