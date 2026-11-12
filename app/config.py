"""Application configuration.

All settings are read from environment variables (prefixed with ``NUAGE_``)
or from a ``.env`` file at the project root. Nothing secret is hard-coded in
the source: passwords live in ``.env``, which is excluded from Git
(see ``.env.example`` for the template).

This follows the "Config" factor of the Twelve-Factor App methodology:
the same code can run in development, test and production; only the
environment changes.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GIB = 1024**3  # one gibibyte, in bytes


class Settings(BaseSettings):
    """Typed settings object.

    Pydantic validates every value at startup: a missing or malformed
    variable (e.g. ``NUAGE_NUM_CTX=abc``) makes the server refuse to start
    with a clear error, instead of crashing later at runtime.
    """

    model_config = SettingsConfigDict(
        env_prefix="NUAGE_",  # NUAGE_DATABASE_URL -> database_url
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore unrelated variables in .env (e.g. POSTGRES_*)
    )

    # --- Database ------------------------------------------------------------
    # URL used by the running application. It connects with the restricted
    # "nuage_app" role, which can only read/write rows (no DROP, no ALTER).
    # Format: postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>
    database_url: str = Field(default="postgresql+asyncpg://nuage_app:change-me@127.0.0.1:5432/nuage_libre")
    # URL used only by Alembic migrations. It connects with the "nuage_owner"
    # role, which owns the schema and is allowed to create/alter tables.
    migration_database_url: str = Field(
        default="postgresql+asyncpg://nuage_owner:change-me@127.0.0.1:5432/nuage_libre"
    )
    # Print every SQL statement in the console (useful for learning/debugging).
    sql_echo: bool = False

    # --- AI engine (Ollama) ----------------------------------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    # Q4_K_M quantization: ~17 GB of VRAM (vs ~23 GB for Q6_K), which leaves
    # room for the context window on the 24 GB RX 7900 XTX and avoids OOM.
    model: str = "hf.co/0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF:Q4_K_M"
    # Context window in tokens. The KV cache grows linearly with this value:
    # measured ~18 GB total VRAM at 16k for the model above. Too high => OOM.
    num_ctx: int = Field(default=16384, ge=512, le=262144)
    # Maximum time (seconds) to wait between two chunks coming from Ollama.
    ollama_read_timeout: float = 300.0

    # --- Chat behaviour ------------------------------------------------------
    # Upper bound on how many previous messages are re-sent to the model.
    # Keeps the prompt inside the context window and limits GPU work
    # (eco-design: we do not re-process an unbounded history).
    max_history_messages: int = Field(default=50, ge=1)
    # Maximum length of a single user message, in characters.
    max_message_length: int = Field(default=100_000, ge=1)

    # --- Private cloud (file storage) -------------------------------------
    # Folder where uploaded files are physically stored, on the 2 TB Lexar
    # NVMe of the Bazzite server. A dedicated sub-folder is used rather than
    # the root of data_nuage_libre/, which already holds the personal files
    # shared over Samba: the application must own its folder entirely
    # (opaque UUID file names, no manual edits). See docs/CLOUD_STORAGE.md.
    storage_dir: Path = Path("/var/mnt/Lexar2To/data_nuage_libre/cloud_storage")
    # Largest accepted upload. Checked against Content-Length BEFORE reading
    # the body (HTTP 413), and again while the bytes are streamed to disk.
    max_upload_bytes: int = Field(default=2 * GIB, ge=1)
    # Free space that must remain on the disk after an upload (HTTP 507
    # otherwise), so the cloud can never fill the disk completely.
    min_free_disk_bytes: int = Field(default=2 * GIB, ge=0)

    @field_validator("storage_dir")
    @classmethod
    def storage_dir_must_be_absolute(cls, value: Path) -> Path:
        """A relative path would depend on the folder uvicorn is started
        from: the same configuration could then write to two different
        places. Refusing it at startup removes that ambiguity."""
        if not value.is_absolute():
            raise ValueError("NUAGE_STORAGE_DIR must be an absolute path.")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the settings singleton.

    ``lru_cache`` ensures the environment / .env file is parsed only once.
    It is also used as a FastAPI dependency, which lets tests replace it
    with ``app.dependency_overrides[get_settings] = ...``.
    """
    return Settings()
