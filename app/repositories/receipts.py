"""Журнал квитанций и дедупликация.

Сюда пишется каждая принятая сервисом квитанция. Уникальное ограничение
на (operation_id, provider_payment_id, result) — механизм дедупликации:
если INSERT не вернул строку, значит ровно такая квитанция уже
обработана и второй переход фиксировать нельзя.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ReceiptResult
from app.domain.models import Receipt


async def try_record(
    session: AsyncSession,
    *,
    operation_id: str,
    provider_payment_id: str,
    result: ReceiptResult,
    message: str | None,
    occurred_at: datetime | None,
    applied: bool,
    ignore_reason: str | None = None,
) -> int | None:
    """Фиксирует квитанцию.

    Возвращает id новой строки либо ``None``, если идентичная квитанция
    уже сохранена — тогда вызывающий код обязан пропустить смену состояния.
    """
    stmt = (
        pg_insert(Receipt)
        .values(
            operation_id=operation_id,
            provider_payment_id=provider_payment_id,
            result=result,
            message=message,
            occurred_at=occurred_at,
            applied=applied,
            ignore_reason=ignore_reason,
        )
        .on_conflict_do_nothing(constraint="uq_receipts_operation_payment_result")
        .returning(Receipt.id)
    )
    insert_result = await session.execute(stmt)
    row_id = insert_result.scalar_one_or_none()
    return int(row_id) if row_id is not None else None


async def list_for_operation(session: AsyncSession, operation_id: str) -> list[Receipt]:
    result = await session.execute(
        select(Receipt)
        .where(Receipt.operation_id == operation_id)
        .order_by(Receipt.received_at, Receipt.id)
    )
    return list(result.scalars().all())


async def has_applied_receipt(session: AsyncSession, operation_id: str) -> bool:
    """True, если исход операции уже определён какой-то квитанцией."""
    result = await session.execute(
        select(Receipt.id)
        .where(Receipt.operation_id == operation_id, Receipt.applied.is_(True))
        .limit(1)
    )
    return result.scalar_one_or_none() is not None
