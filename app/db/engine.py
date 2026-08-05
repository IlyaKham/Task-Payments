"""Асинхронный движок и фабрика сессий.

Сессии намеренно короткоживущие. Ни одна транзакция БД не должна
оставаться открытой на время внешнего HTTP-вызова: callback провайдера
может прийти раньше его же ответа, и удерживаемая блокировка приведёт
к взаимной блокировке потока обработки.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=settings.db_pool_pre_ping,
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакционная область: коммит при успехе, откат при ошибке."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def dispose_engine() -> None:
    engine = get_engine()
    await engine.dispose()
