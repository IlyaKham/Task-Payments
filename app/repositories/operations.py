"""Переходы состояний операции.

Каждая смена статуса — условный UPDATE, который несёт своё предусловие
прямо в WHERE. Нигде нет связки «прочитал — проверил — записал», поэтому
N конкурентных вызовов дают ровно одного победителя без явной блокировки
и без мьютекса на уровне приложения.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TERMINAL_STATUSES, OperationStatus
from app.domain.errors import OperationNotFound
from app.domain.models import Operation


async def create(
    session: AsyncSession,
    *,
    operation_id: str,
    amount: Decimal,
    currency: str,
    description: str | None,
) -> Operation | None:
    """Вставляет новую операцию.

    Возвращает ``None``, если такой id уже существует. Используем
    ON CONFLICT DO NOTHING вместо перехвата IntegrityError, чтобы внешняя
    транзакция осталась рабочей: в PostgreSQL нарушение ограничения её
    прерывает.
    """
    stmt = (
        pg_insert(Operation)
        .values(
            operation_id=operation_id,
            amount=amount,
            currency=currency,
            description=description,
            status=OperationStatus.CREATED,
        )
        .on_conflict_do_nothing(index_elements=[Operation.operation_id])
        .returning(Operation)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get(session: AsyncSession, operation_id: str) -> Operation | None:
    result = await session.execute(
        select(Operation).where(Operation.operation_id == operation_id)
    )
    return result.scalar_one_or_none()


async def get_or_raise(session: AsyncSession, operation_id: str) -> Operation:
    operation = await get(session, operation_id)
    if operation is None:
        raise OperationNotFound(operation_id)
    return operation


async def get_for_update(session: AsyncSession, operation_id: str) -> Operation | None:
    """Читает строку с блокировкой на запись.

    Используется только при обработке квитанции — это короткая транзакция
    без обращений в сеть. Такая блокировка никогда не удерживается на
    время внешнего HTTP-вызова.
    """
    result = await session.execute(
        select(Operation).where(Operation.operation_id == operation_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def try_begin_processing(session: AsyncSession, operation_id: str) -> bool:
    """Атомарный переход CREATED -> PROCESSING.

    Возвращает True единственному вызову, выигравшему гонку; все остальные
    конкурентные submit получают False и просто отдают текущее состояние.
    """
    result = await session.execute(
        update(Operation)
        .where(
            Operation.operation_id == operation_id,
            Operation.status == OperationStatus.CREATED,
        )
        .values(status=OperationStatus.PROCESSING)
    )
    return result.rowcount == 1


async def link_provider_payment(
    session: AsyncSession, operation_id: str, provider_payment_id: str
) -> bool:
    """Привязывает идентификатор платежа провайдера, один раз и навсегда.

    Намеренно не трогает ``status``: запоздалый ответ 202 не должен
    вытаскивать операцию из COMPLETED или REJECTED. Возвращает False, если
    связь уже была установлена (например, ранней квитанцией).
    """
    result = await session.execute(
        update(Operation)
        .where(
            Operation.operation_id == operation_id,
            Operation.provider_payment_id.is_(None),
        )
        .values(provider_payment_id=provider_payment_id)
    )
    return result.rowcount == 1


async def try_finalize(
    session: AsyncSession, operation_id: str, to_status: OperationStatus
) -> bool:
    """Атомарный переход PROCESSING -> COMPLETED/REJECTED.

    Финальные статусы поглощающие: условие WHERE не даст сдвинуть уже
    завершённую операцию, поэтому запоздалая конфликтующая квитанция не
    может перевернуть результат.
    """
    if to_status not in TERMINAL_STATUSES:
        raise ValueError(f"{to_status} не является финальным статусом")

    result = await session.execute(
        update(Operation)
        .where(
            Operation.operation_id == operation_id,
            Operation.status == OperationStatus.PROCESSING,
        )
        .values(status=to_status)
    )
    return result.rowcount == 1


async def list_unfinished(session: AsyncSession) -> list[Operation]:
    """Операции, всё ещё ожидающие квитанцию. Нужны для восстановления."""
    result = await session.execute(
        select(Operation).where(Operation.status == OperationStatus.PROCESSING)
    )
    return list(result.scalars().all())
