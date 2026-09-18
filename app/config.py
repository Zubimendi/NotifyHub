from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://notifyhub:notifyhub@localhost:5432/notifyhub"
    redis_url: str = "redis://localhost:6379/0"
    http_port: int = 8000

    queueline_base_url: str = "http://localhost:8080"
    queueline_dispatch_queue: str = "notify-dispatch"

    email_provider_chain: str = "http://localhost:9101,http://localhost:9102"
    sms_provider_chain: str = "http://localhost:9103"
    push_provider_chain: str = "http://localhost:9104"
    provider_timeout_ms: int = 3000
    max_same_provider_retries: int = 3

    default_digest_window_minutes: int = 60
    digest_sweep_interval_ms: int = 15000
    flushing_stuck_timeout_seconds: int = 300

    unsubscribe_token_secret: str = "dev-secret-change-me"

    def chain_urls(self, channel: str) -> list[str]:
        raw = {
            "EMAIL": self.email_provider_chain,
            "SMS": self.sms_provider_chain,
            "PUSH": self.push_provider_chain,
        }.get(channel.upper(), "")
        return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
