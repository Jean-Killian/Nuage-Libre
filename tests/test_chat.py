"""Integration tests for POST /api/chat.

Each test follows the Arrange / Act / Assert structure and documents, in
its name and docstring, the requirement it verifies. The IDs (T-CHAT-xx)
match the test plan in docs/TEST_PLAN.md.
"""

import uuid

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Conversation, Message
from app.services.ollama_client import OllamaError
from tests.conftest import parse_events


async def count_rows() -> tuple[int, int]:
    async with SessionLocal() as session:
        conversations = await session.scalar(select(func.count()).select_from(Conversation))
        messages = await session.scalar(select(func.count()).select_from(Message))
    return conversations, messages


async def test_new_conversation_streams_events_in_order(client):
    """T-CHAT-01: a first message creates a conversation and streams the answer."""
    response = await client.post("/api/chat", json={"message": "Hi, I am Killian"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = parse_events(response)
    types = [e["type"] for e in events]
    assert types == ["conversation", "thinking", "content", "content", "done"]
    assert events[0]["is_new"] is True
    assert events[0]["title"] == "Hi, I am Killian"
    assert "".join(e["text"] for e in events if e["type"] == "content") == "Hello Killian!"
    assert events[-1]["token_count"] == 3
    assert events[-1]["generation_ms"] == 150


async def test_exchange_is_persisted(client):
    """T-CHAT-02: the question and the answer (with reasoning and stats) are saved."""
    events = parse_events(await client.post("/api/chat", json={"message": "Hi"}))
    conversation_id = events[0]["id"]

    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()

    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    user, assistant = detail["messages"]
    assert user["content"] == "Hi"
    assert assistant["content"] == "Hello Killian!"
    assert assistant["thinking"] == "Let me think."
    assert assistant["token_count"] == 3
    # The answer is always dated after the question.
    assert assistant["created_at"] > user["created_at"]
    assert assistant["id"] == events[-1]["message_id"]


async def test_follow_up_sends_history_to_model(client, fake_ollama):
    """T-CHAT-03: the model receives the previous messages (conversation memory)."""
    first = parse_events(await client.post("/api/chat", json={"message": "My name is Killian"}))
    conversation_id = first[0]["id"]

    second = parse_events(
        await client.post("/api/chat", json={"conversation_id": conversation_id, "message": "Who am I?"})
    )

    assert second[0]["is_new"] is False
    assert second[0]["id"] == conversation_id
    prompt = fake_ollama.calls[-1]["messages"]
    assert prompt == [
        {"role": "user", "content": "My name is Killian"},
        {"role": "assistant", "content": "Hello Killian!"},
        {"role": "user", "content": "Who am I?"},
    ]
    # Reasoning of previous turns is NOT sent back (saves tokens).
    assert all("thinking" not in m for m in prompt)


async def test_history_is_limited(client, fake_ollama, override_settings):
    """T-CHAT-04: only the N most recent messages are sent (context window protection)."""
    override_settings(max_history_messages=3)
    conversation_id = parse_events(await client.post("/api/chat", json={"message": "one"}))[0]["id"]
    await client.post("/api/chat", json={"conversation_id": conversation_id, "message": "two"})

    await client.post("/api/chat", json={"conversation_id": conversation_id, "message": "three"})

    prompt = fake_ollama.calls[-1]["messages"]
    assert len(prompt) == 3  # 2 most recent history messages + the new one
    assert [m["content"] for m in prompt] == ["two", "Hello Killian!", "three"]


async def test_model_error_saves_nothing(client, fake_ollama):
    """T-CHAT-05: if generation fails, an error event is sent and nothing is written (atomicity)."""
    fake_ollama.error = OllamaError("Ollama error: out of memory")

    events = parse_events(await client.post("/api/chat", json={"message": "Hi"}))

    assert [e["type"] for e in events] == ["conversation", "error"]
    assert events[-1]["text"] == "Ollama error: out of memory"
    assert await count_rows() == (0, 0)


async def test_unknown_conversation_returns_404(client):
    """T-CHAT-06: continuing a conversation that does not exist is rejected."""
    response = await client.post("/api/chat", json={"conversation_id": str(uuid.uuid4()), "message": "Hi"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Conversation not found."}


async def test_blank_message_is_rejected(client, fake_ollama):
    """T-CHAT-07: a message made only of whitespace is refused before reaching the model."""
    response = await client.post("/api/chat", json={"message": "   \n  "})

    assert response.status_code == 422
    assert fake_ollama.calls == []


async def test_too_long_message_is_rejected(client, fake_ollama, override_settings):
    """T-CHAT-08: the configurable maximum message length is enforced."""
    override_settings(max_message_length=10)

    response = await client.post("/api/chat", json={"message": "x" * 11})

    assert response.status_code == 413
    assert fake_ollama.calls == []


async def test_malformed_conversation_id_is_rejected(client):
    """T-CHAT-09: a conversation_id that is not a UUID is refused by validation."""
    response = await client.post("/api/chat", json={"conversation_id": "1 OR 1=1", "message": "Hi"})

    assert response.status_code == 422


async def test_sql_injection_payload_is_stored_as_plain_text(client):
    """T-SEC-01: SQL in user input is stored verbatim and never executed.

    Queries are parameterised: the text travels separately from the SQL
    statement, so PostgreSQL treats it as data, not as code.
    """
    payload = "'); DROP TABLE messages; --"

    events = parse_events(await client.post("/api/chat", json={"message": payload}))

    detail = (await client.get(f"/api/conversations/{events[0]['id']}")).json()
    assert detail["messages"][0]["content"] == payload
    assert await count_rows() == (1, 2)  # the messages table still exists


async def test_long_first_message_gets_truncated_title(client):
    """T-CHAT-10: the conversation title is the first line, truncated to 60 characters."""
    message = "A" * 100 + "\nsecond line"

    events = parse_events(await client.post("/api/chat", json={"message": message}))

    assert len(events[0]["title"]) == 60
    assert events[0]["title"].endswith("…")
