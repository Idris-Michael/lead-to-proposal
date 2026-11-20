"""All configuration comes from the environment (12-factor). Secrets have no defaults: a missing
secret stops the process at start-up rather than running open."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ltp:ltp@localhost:5432/ltp"
    webhook_secret: SecretStr
    operator_token: SecretStr = Field(min_length=24)

    hubspot_mode: Literal["mock", "live"] = "mock"
    hubspot_token: SecretStr = SecretStr("")
    hubspot_fake_429_ratio: float = Field(default=0.0, ge=0, le=0.9)

    llm_mode: Literal["claude_cli", "offline"] = "claude_cli"
    claude_cli_path: str | None = None
    qualify_model: str = "claude-haiku-5-5"
    draft_model: str = "claude-sonnet-5-5"

    approval_mode: Literal["local", "gateway"] = "local"
    gateway_url: str = "http://localhost:8787"
    gateway_agent_key: SecretStr = SecretStr("")
    gateway_callback_secret: SecretStr = SecretStr("")

    rate_limit_per_minute: int = Field(default=60, ge=1)
    worker_poll_s: float = 1.0
    worker_lease_s: int = 300
    worker_max_attempts: int = 5

    otel_exporter_otlp_endpoint: str | None = None
    service_name: str = "lead-to-proposal"

    @model_validator(mode="after")
    def check_mode_secrets(self) -> Settings:
        if self.hubspot_mode == "live" and not self.hubspot_token.get_secret_value():
            raise ValueError("HUBSPOT_MODE=live needs HUBSPOT_TOKEN")
        if self.approval_mode == "gateway" and not (
            self.gateway_agent_key.get_secret_value() and self.gateway_callback_secret.get_secret_value()
        ):
            raise ValueError("APPROVAL_MODE=gateway needs GATEWAY_AGENT_KEY and GATEWAY_CALLBACK_SECRET")
        return self
