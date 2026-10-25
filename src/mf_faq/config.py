"""Settings loaded from the environment and `.env` (P0.11, arch §15)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MF_FAQ_",
        extra="ignore",
        populate_by_name=True,
    )

    # LLM (Groq). The key has no prefix, matching the Groq SDK's own convention.
    groq_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GROQ_API_KEY", "groq_api_key")
    )
    model: str = "openai/gpt-oss-120b"
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    # Reasoning (low) + a <=3-sentence JSON answer. Kept well under the plan's 4096: the free-tier
    # budget is 8K tokens/minute, so a big ceiling would make every request look expensive.
    max_completion_tokens: int = Field(default=1024, ge=256)
    llm_timeout_s: float = Field(default=30.0, gt=0)
    # Groq limits for openai/gpt-oss-120b on this account; the client-side limiter (P5.6) stays
    # inside them instead of waiting for 429s. Set a limit to 0 to switch it off.
    llm_rpm: int = Field(default=30, ge=0)
    llm_tpm: int = Field(default=8_000, ge=0)
    llm_rpd: int = Field(default=1_000, ge=0)
    llm_tpd: int = Field(default=200_000, ge=0)
    # Longest the limiter may make a request wait before it gives up (reply: `error` template).
    llm_max_wait_s: float = Field(default=10.0, ge=0)

    # Retrieval
    embed_model: str = "BAAI/bge-small-en-v1.5"
    # Hugging Face commit of the model. Pinned so the Docker image, the refresh workflow and a dev
    # machine embed with the same weights: an index only loads with the revision it was built
    # with (P3.3). None = the repo's latest (fine locally, not for deployment).
    embed_model_revision: str | None = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    # Prepended to queries only, never to chunks (bge model card). Recorded in the index (P3.1).
    embed_query_instruction: str = "Represent this sentence for searching relevant passages: "
    # Chunks are split to fit this many embedding-model tokens, header included (P3.0).
    holdings_max_tokens: int = Field(default=400, ge=64)
    retrieval_top_k: int = Field(default=4, ge=1)
    # Dense-cosine safety floor (P3.4 layer D), not the main not-found gate. P3.6: the in-corpus
    # eval minimum (0.729, bge-small + covers line, 25 Oct 2026) minus a 0.03-0.05 margin. It is
    # specific to the model, query instruction and chunk format: after changing any of them,
    # re-derive it from the "τ range" line of `mf-faq eval --retrieval-only`.
    retrieval_threshold: float = Field(default=0.69, ge=0.0, le=1.0)

    # Threads
    history_turns: int = Field(default=6, ge=0)
    thread_ttl_days: int = Field(default=30, ge=1)

    # API (P6.6): per-IP limit on the endpoints that write (slowapi syntax).
    api_rate_limit: str = "20/minute"

    # Daily refresh (P7.6). The server re-reads `data/index/current` this often and reloads on
    # a change (0 = never). `mf-faq pull-index` downloads the newest index from the private
    # repo's releases with a read-only token.
    index_reload_s: float = Field(default=300.0, ge=0)
    github_repo: str | None = None  # "owner/name"
    github_token: SecretStr | None = None
    # A source that stays `stale` this many days in a row fails the refresh run (P7.5).
    stale_alert_days: int = Field(default=3, ge=1)

    # Templates fall back to this source when no scheme is resolved (P4.3).
    default_source_id: str = "groww-hdfc-large-cap"

    # Paths and logging
    corpus_dir: Path = Path("corpus")
    data_dir: Path = Path("data")
    eval_dir: Path = Path("tests/eval")  # golden + retrieval query sets (P3.5)
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        """SQLite file for threads, messages and the ingest's `sources` table (arch §8.1)."""
        return self.data_dir / "app.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
