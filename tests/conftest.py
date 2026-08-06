"""Общая обвязка тестов.

Адрес тестовой базы подменяется **до** импорта модулей приложения:
``app.db.engine`` кэширует движок, а сервисный код ходит в базу через
глобальный ``session_scope``. Так тесты гоняют настоящий код целиком,
а не его копию с подставленной сессией — и при этом физически не могут
задеть рабочую базу.
"""

from __future__ import annotations

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://payments:payments@localhost:5432/payments_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import asyncio  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.engine import get_engine, session_scope  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TABLES = "operations, event_outbox, events, receipts"


def alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


async def _probe_database() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Схема из миграций, один раз на прогон.

    Схема поднимается именно alembic'ом, а не ``create_all``: тесты должны
    работать на той же схеме, которую получит контейнер.

    Пропуск возможен только по недоступности базы — она проверяется
    отдельной пробой. Сломанная миграция обязана падать, а не тихо
    превращаться в «пропущено».
    """
    try:
        asyncio.run(_probe_database())
    except Exception as exc:  # pragma: no cover - зависит от окружения
        pytest.skip(
            f"PostgreSQL недоступен по {TEST_DATABASE_URL}: {exc!r}. "
            "Поднимите базу или задайте TEST_DATABASE_URL."
        )

    command.upgrade(alembic_config(), "head")


@pytest.fixture
async def clean_database(migrated_database: None) -> AsyncIterator[None]:
    """Пустые таблицы перед каждым тестом."""
    engine = get_engine()

    # Движок закэширован на весь процесс, а event loop у каждого теста свой.
    # Соединение, оставшееся от чужого цикла, падает с невнятным
    # "'NoneType' object has no attribute 'send'", поэтому пул сбрасывается
    # на обеих границах теста.
    await engine.dispose()
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))

    try:
        yield
    finally:
        await engine.dispose()


@pytest.fixture
async def session(clean_database: None) -> AsyncIterator[AsyncSession]:
    """Сессия для подготовки данных и проверок в тесте."""
    async with session_scope() as active:
        yield active


@pytest.fixture
def fast_settings() -> Settings:
    """Настройки без пауз: backoff в тестах только мешает."""
    return Settings(
        database_url=TEST_DATABASE_URL,
        provider_max_attempts=3,
        provider_backoff_base=0.0,
        provider_backoff_max=0.0,
        provider_backoff_jitter=0.0,
        dispatcher_batch_size=32,
        dispatcher_concurrency=8,
        intent_lease_seconds=60,
    )
