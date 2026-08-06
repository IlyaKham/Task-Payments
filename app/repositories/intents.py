"""Намерения отправки — транзакционный outbox (таблица ``event_outbox``).

Захват использует ``FOR UPDATE ... SKIP LOCKED``: несколько задач
диспетчера (или несколько экземпляров сервиса) могут опрашивать одну
таблицу и никогда не отдадут одно намерение двум воркерам сразу.

Транзакция захвата коммитится *до* начала HTTP-вызова, поэтому на время
работы с сетью блокировка не удерживается. Устойчивость к падению даёт
``lease_expires_at``: намерение в IN_FLIGHT с истёкшей арендой снова
становится доступным для захвата. Повторная отправка безопасна, потому
что переиспользует сохранённый ``idempotency_key`` и побайтово тот же
``request_body``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import IntentState, OperationStatus
from app.domain.models import Operation, SubmitIntent
from app.repositories._sql import execute_rowcount


async def create(
    session: AsyncSession,
    *,
    operation_id: str,
    idempotency_key: str,
    request_body: str,
) -> bool:
    """Сохраняет намерение вызвать провайдера. Идемпотентно."""
    stmt = (
        pg_insert(SubmitIntent)
        .values(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            request_body=request_body,
            state=IntentState.PENDING,
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=[SubmitIntent.operation_id])
        .returning(SubmitIntent.operation_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def get(session: AsyncSession, operation_id: str) -> SubmitIntent | None:
    result = await session.execute(
        select(SubmitIntent).where(SubmitIntent.operation_id == operation_id)
    )
    return result.scalar_one_or_none()


async def claim_due(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
    lease_seconds: int,
) -> list[SubmitIntent]:
    """Атомарно забирает до ``limit`` намерений, которым пора отправляться.

    «Пора» — это либо PENDING с истёкшим backoff, либо IN_FLIGHT с
    протухшей арендой (предыдущая попытка оборвалась на полпути).
    """
    due = (
        select(SubmitIntent.operation_id)
        .where(
            or_(
                (SubmitIntent.state == IntentState.PENDING)
                & (SubmitIntent.next_attempt_at <= now),
                (SubmitIntent.state == IntentState.IN_FLIGHT)
                & (SubmitIntent.lease_expires_at.is_not(None))
                & (SubmitIntent.lease_expires_at <= now),
            )
        )
        .order_by(SubmitIntent.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    stmt = (
        update(SubmitIntent)
        .where(SubmitIntent.operation_id.in_(due))
        .values(
            state=IntentState.IN_FLIGHT,
            attempts=SubmitIntent.attempts + 1,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        .returning(SubmitIntent)
        # Синхронизировать сессию нечем и незачем: захваченные строки
        # тут же уезжают в обычные значения, а сессия закрывается.
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_done(session: AsyncSession, operation_id: str) -> None:
    """Провайдер подтвердил приём платежа, дальнейшие попытки не нужны."""
    await session.execute(
        update(SubmitIntent)
        .where(SubmitIntent.operation_id == operation_id)
        .values(state=IntentState.DONE, lease_expires_at=None, last_error=None)
    )


async def reschedule(
    session: AsyncSession,
    operation_id: str,
    *,
    next_attempt_at: datetime,
    error: str,
) -> None:
    """Повторяемый сбой: возвращаем в PENDING с задержкой.

    Сама операция остаётся в PROCESSING — транспортный сбой не является
    отказом, платёж вполне мог быть уже принят провайдером.

    Условие ``state == IN_FLIGHT`` обязательно: пока шёл сетевой вызов,
    квитанция могла прийти и перевести намерение в DONE. Без предусловия
    неудачная попытка воскресила бы завершённую работу.
    """
    await session.execute(
        update(SubmitIntent)
        .where(
            SubmitIntent.operation_id == operation_id,
            SubmitIntent.state == IntentState.IN_FLIGHT,
        )
        .values(
            state=IntentState.PENDING,
            next_attempt_at=next_attempt_at,
            lease_expires_at=None,
            last_error=error[:2000],
        )
    )


async def mark_failed(session: AsyncSession, operation_id: str, error: str) -> None:
    """Попытки исчерпаны либо ошибка неповторяемая.

    Статус операции всё равно не меняем: это право есть только у
    квитанции. Операция остаётся в PROCESSING и видна как незавершённая.

    Предусловие по состоянию — то же, что и в ``reschedule``: пришедшая
    во время вызова квитанция уже могла закрыть намерение.
    """
    await session.execute(
        update(SubmitIntent)
        .where(
            SubmitIntent.operation_id == operation_id,
            SubmitIntent.state == IntentState.IN_FLIGHT,
        )
        .values(state=IntentState.FAILED, lease_expires_at=None, last_error=error[:2000])
    )


async def release_for_retry(session: AsyncSession, operation_id: str) -> None:
    """Возвращает захваченное намерение в очередь, не тратя backoff.

    Нужно при корректной остановке процесса, чтобы прерванная попытка
    возобновилась сразу после запуска.
    """
    await session.execute(
        update(SubmitIntent)
        .where(
            SubmitIntent.operation_id == operation_id,
            SubmitIntent.state == IntentState.IN_FLIGHT,
        )
        .values(state=IntentState.PENDING, lease_expires_at=None)
    )


async def revive_failed(session: AsyncSession) -> int:
    """При старте возвращает намерения из FAILED обратно в очередь.

    Перезапуск — новый шанс: провайдер мог восстановиться, а ключ
    идемпотентности гарантирует, что второго платежа не появится.

    Оживляем только намерения незавершённых операций. Если исход уже
    известен из квитанции, повторять вызов незачем.
    """
    unfinished = (
        select(Operation.operation_id)
        .where(Operation.status == OperationStatus.PROCESSING)
        .scalar_subquery()
    )
    return await execute_rowcount(
        session,
        update(SubmitIntent)
        .where(
            SubmitIntent.state == IntentState.FAILED,
            SubmitIntent.operation_id.in_(unfinished),
        )
        .values(state=IntentState.PENDING, attempts=0, lease_expires_at=None),
    )


async def release_all_in_flight(session: AsyncSession) -> int:
    """При старте отпускает намерения, брошенные предыдущим процессом.

    Аренда павшего процесса истечёт сама, но ждать её нечестно по времени:
    перезапуск во время обработки — штатный сценарий, и отправка должна
    продолжиться сразу. Здесь используется допущение, что экземпляр сервиса
    один (как в compose задания). Даже если оно нарушится, второго платежа
    не будет: повтор уходит с тем же ключом идемпотентности.
    """
    return await execute_rowcount(
        session,
        update(SubmitIntent)
        .where(SubmitIntent.state == IntentState.IN_FLIGHT)
        .values(state=IntentState.PENDING, lease_expires_at=None),
    )


async def list_resumable(session: AsyncSession) -> list[SubmitIntent]:
    """Намерения, отправка которых будет продолжена после запуска."""
    unfinished = (
        select(Operation.operation_id)
        .where(Operation.status == OperationStatus.PROCESSING)
        .scalar_subquery()
    )
    result = await session.execute(
        select(SubmitIntent)
        .where(
            SubmitIntent.state == IntentState.PENDING,
            SubmitIntent.operation_id.in_(unfinished),
        )
        .order_by(SubmitIntent.created_at)
    )
    return list(result.scalars().all())
