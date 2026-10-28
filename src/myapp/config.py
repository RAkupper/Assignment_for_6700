# Read service configuration from environment variables.

# Imports
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    FilePath,
    HttpUrl,
    SecretStr,
    StringConstraints,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


# Define typed settings for the model backend
class ModelSettings(BaseSettings):
    """Where the language model runs and how the service talks to it.

    Every field is read from an ``LLM_``-prefixed environment variable (or ``.env``),
    so switching between a local Ollama container, a vLLM server and a hosted
    OpenAI-compatible API is a configuration change, not a code change.
    """

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
        extra="ignore",
    )

    # "echo" needs no server (tests, CI, first local run); "openai" calls any
    # OpenAI-compatible /chat/completions endpoint (Ollama, vLLM, hosted APIs).
    provider: Literal["echo", "openai"] = "echo"
    base_url: HttpUrl = HttpUrl("http://localhost:11434/v1")
    model: str = Field(default="qwen2.5:1.5b-instruct", min_length=1)
    api_key: SecretStr | None = None
    timeout_s: float = Field(default=120.0, gt=0)
    connect_timeout_s: float = Field(default=5.0, gt=0)
    max_tokens: int = Field(default=512, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    # Read by the model server container, not by the app process: they live here
    # so the whole stack is configured from one documented set of variables.
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    cache_dir: Path = Path("/models")


# Define typed settings for the service
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
        extra="ignore",
    )
    name: str = Field(default="assignment-for-6700", min_length=1)
    environment: Literal["development", "production"] = "development"
    debug: bool = False
    model_path: FilePath = Path("models/digits.joblib")
    llm: ModelSettings = Field(default_factory=ModelSettings)

    @model_validator(mode="after")
    def reject_debug_in_production(self) -> Self:
        """Reject debug mode in production before the app starts"""
        if self.environment == "production" and self.debug:
            raise ValueError("APP_DEBUG must be false in production")
        return self

    @model_validator(mode="after")
    def reject_echo_model_in_production(self) -> Self:
        """Refuse to serve the placeholder echo model in production"""
        if self.environment == "production" and self.llm.provider == "echo":
            raise ValueError("LLM_PROVIDER must not be 'echo' in production")
        return self


CfrPart = Annotated[str, StringConstraints(pattern=r"^\d+ CFR \d+[a-z]?$")]


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INGEST_",
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
        extra="ignore",
    )
    data_dir: Path = Path("data")  # bind-mounted at /data on the instance
    ecfr_base_url: HttpUrl = HttpUrl("https://www.ecfr.gov")
    ecfr_parts: list[CfrPart] = Field(  # JSON list in the environment
        default=["8 CFR 214", "8 CFR 274a", "22 CFR 41", "22 CFR 62"],
        min_length=1,
    )
    federal_register_base_url: HttpUrl = HttpUrl(
        "https://www.federalregister.gov/api/v1"
    )
    federal_register_since: date = date(2020, 1, 1)  # first backfill only
    ecfr_since: date = date(
        2020, 1, 1
    )  # history window start; one snapshot per amendment
    user_agent: str = Field(
        default="dsan6700-ingest (Georgetown DSAN student project)", min_length=1
    )
    request_timeout_s: float = Field(default=30.0, gt=0)
    min_request_interval_s: float = Field(default=1.0, ge=0)  # per-host politeness
    max_retries: int = Field(default=3, ge=0, le=10)

    @model_validator(mode="after")
    def reject_duplicate_parts(self) -> Self:
        if len(set(self.ecfr_parts)) != len(self.ecfr_parts):
            raise ValueError("INGEST_ECFR_PARTS has duplicates")
        return self
