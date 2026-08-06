"""Зависимости FastAPI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import session_scope


async def get_session() -> AsyncIterator[AsyncSession]:
    """Одна транзакция на запрос.

    Коммит происходит при успешном завершении обработчика, откат — при
    любом исключении, включая доменные ошибки. Именно это даёт требуемое
    ТЗ правило «обработка квитанции и изменение состояния выполняются
    одной транзакцией».
    """
    async with session_scope() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
