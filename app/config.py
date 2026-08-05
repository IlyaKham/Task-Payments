"""Настройки приложения, читаются из переменных окружения."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Инфраструктура ---------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://payments:payments@localhost:5432/payments",
        description="Async DSN PostgreSQL для SQLAlchemy.",
    )
    provider_url: str = Field(
        default="http://localhost:8081",
        description="Базовый адрес внешнего платёжного провайдера.",
    )
    service_host: str = "0.0.0.0"
    service_port: int = 8080

    # --- Пул соединений с БД ----------------------------------------------
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_pre_ping: bool = True
    db_echo: bool = False

    # --- HTTP-вызовы провайдера -------------------------------------------
    provider_connect_timeout: float = 3.0
    provider_read_timeout: float = 10.0
    provider_max_attempts: int = 10
    provider_backoff_base: float = 0.5
    provider_backoff_max: float = 15.0
    provider_backoff_jitter: float = 0.3

    # --- Фоновый диспетчер ------------------------------------------------
    dispatcher_enabled: bool = True
    dispatcher_poll_interval: float = 1.0
    dispatcher_batch_size: int = 16
    dispatcher_concurrency: int = 8
    # Сколько намерение может оставаться в IN_FLIGHT, прежде чем другой
    # воркер (или этот же после перезапуска) имеет право забрать его снова.
    intent_lease_seconds: int = 60
    shutdown_grace_seconds: float = 10.0

    # --- Логирование ------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @property
    def supported_currencies(self) -> frozenset[str]:
        return frozenset({"RUB"})


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
