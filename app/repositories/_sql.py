"""Мелкие помощники поверх SQLAlchemy."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Executable
from sqlalchemy.ext.asyncio import AsyncSession


async def execute_rowcount(session: AsyncSession, statement: Executable) -> int:
    """Выполняет DML и возвращает число затронутых строк.

    ``AsyncSession.execute`` типизирован как ``Result``, у которого нет
    ``rowcount``: атрибут объявлен в ``CursorResult``. Для DML результат
    всегда им и является, поэтому приведение безопасно — и пусть оно
    останется в одном месте, а не расползается по репозиториям.
    """
    result = await session.execute(statement)
    return cast("CursorResult[Any]", result).rowcount
