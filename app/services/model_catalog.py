"""Catalogue of the models the user is allowed to chat with.

The browser sends a model name chosen in the drop-down menu. That value comes
from the client, so it is *untrusted*: the catalogue checks it against the list
of models actually installed in Ollama (an allow-list) before it is used.
Anything else is rejected with a clear error instead of being forwarded blindly.
"""

from app.services.ollama_client import ModelInfo, OllamaClient


class UnknownModelError(Exception):
    """The requested model is not installed or cannot be used for chat."""


class ModelCatalog:
    def __init__(self, ollama: OllamaClient, default_model: str) -> None:
        self.ollama = ollama
        self.default_model = default_model

    async def chat_models(self) -> list[ModelInfo]:
        """Installed models that can hold a conversation, smallest first.

        Embedding-only models (e.g. nomic-embed-text, used later for RAG) have
        no "completion" capability and are hidden from the menu.
        Sorting by size puts the fast, light models at the top.
        """
        models = await self.ollama.list_models()
        usable = [m for m in models if "completion" in m.capabilities]
        return sorted(usable, key=lambda m: m.size_bytes)

    async def validate(self, name: str) -> str:
        """Return ``name`` if it is an installed chat model, else raise UnknownModelError.

        Raises OllamaError if Ollama itself cannot be reached.
        """
        if name not in {m.name for m in await self.chat_models()}:
            raise UnknownModelError(name)
        return name
