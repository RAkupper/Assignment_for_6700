# Read service configuration from environment variables.

# Imports
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Define typed settings for the service
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
    )
    name: str = Field(default="assignment-for-6700", min_length=1)
    environment: Literal["development", "production"] = "development"
    debug: bool = False

    @model_validator(mode="after")
    def reject_debug_in_production(self) -> Self:
        """Reject debug mode in production before the app starts"""
        if self.environment == "production" and self.debug:
            raise ValueError("APP_DEBUG must be false in production")
        return self
