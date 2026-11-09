"""Pydantic schemas (a.k.a. DTOs, Data Transfer Objects).

These classes describe what goes *in* and *out* of the HTTP API. They are
deliberately separate from the ORM models in ``app/models.py``:

- Input schemas validate untrusted client data *before* it reaches the
  business logic (types, lengths, required fields). Invalid requests are
  rejected automatically with HTTP 422 and never touch the database.
- Output schemas choose exactly which fields are exposed, so adding an
  internal column to a table never leaks it through the API by accident.

FastAPI also uses them to generate the interactive documentation at /docs.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Input -------------------------------------------------------------------
class ChatRequest(BaseModel):
    """Body of ``POST /api/chat``."""

    # None => start a new conversation. Otherwise, continue an existing one.
    conversation_id: uuid.UUID | None = None
    # max_length is a first safety net; the configurable limit
    # (NUAGE_MAX_MESSAGE_LENGTH) is checked again in the router.
    message: str = Field(min_length=1, max_length=100_000)
    # Model picked in the menu. None => keep the conversation's model (or the
    # default for a new chat). Checked against installed models in the service.
    model: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("message")
    @classmethod
    def strip_and_reject_blank(cls, value: str) -> str:
        """Refuse messages made only of spaces / newlines."""
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be blank.")
        return value


# --- Output ------------------------------------------------------------------
class MessageOut(BaseModel):
    # from_attributes=True lets Pydantic read values directly from an ORM
    # object (message.content) instead of requiring a dict.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    thinking: str | None
    model: str | None
    token_count: int | None
    generation_ms: int | None
    created_at: datetime


class ConversationSummary(BaseModel):
    """Lightweight representation used by the sidebar list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    model: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationSummary):
    """Full conversation with all its messages."""

    messages: list[MessageOut]


class ModelOut(BaseModel):
    """One entry of the model drop-down menu."""

    name: str
    family: str | None
    parameter_size: str | None
    quantization: str | None
    size_bytes: int
    # Shortcuts for the UI: does the model show its reasoning / accept images?
    thinking: bool
    vision: bool
    is_default: bool


class HealthOut(BaseModel):
    ollama: Literal["up", "down"]
    database: Literal["up", "down"]
    ollama_version: str | None = None
    model: str
