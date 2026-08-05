"""Журнал событий, только на добавление.

``event_id`` должен быть монотонным *в пределах одной операции*, поэтому
глобальная последовательность не подходит. Следующий id вычисляется как
``COALESCE(MAX(event_id), 0) + 1`` прямо внутри INSERT — чтение и запись
выполняются одним оператором.

Две параллельные транзакции всё же могут вычислить один и тот же id;
составной первичный ключ превращает это в IntegrityError вместо дубликата,
а повтор через savepoint ниже разрешает конфликт.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EventType, OperationStatus
from app.domain.models import Event

_MAX_ID_COLLISION_RETRIES = 5


async def append_event(
    session: AsyncSession,
    *,
    operation_id: str,
    event_type: EventType,
    from_status: OperationStatus | None = None,
    to_status: OperationStatus | None = None,
    message: str | None = None,
    occurred_at: datetime | None = None,
) -> int:
    """Добавляет одно событие и возвращает его ``event_id``."""

    next_event_id = (
        select(func.coalesce(func.max(Event.event_id), 0) + 1)
        .where(Event.operation_id == operation_id)
        .scalar_subquery()
    )

    values: dict[str, object] = {
        "operation_id": operation_id,
        "event_id": next_event_id,
        "type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "message": message,
    }
    if occurred_at is not None:
        values["occurred_at"] = occurred_at

    stmt = insert(Event).values(**values).returning(Event.event_id)

    for attempt in range(_MAX_ID_COLLISION_RETRIES):
        try:
            async with session.begin_nested():
                result = await session.execute(stmt)
                return int(result.scalar_one())
        except IntegrityError:
            if attempt == _MAX_ID_COLLISION_RETRIES - 1:
                raise
            continue

    raise RuntimeError("недостижимая ветка")  # pragma: no cover


async def list_events(session: AsyncSession, operation_id: str) -> list[Event]:
    """Полная история в порядке фиксации."""
    result = await session.execute(
        select(Event).where(Event.operation_id == operation_id).order_by(Event.event_id)
    )
    return list(result.scalars().all())
