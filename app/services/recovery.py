"""Восстановление незавершённой работы при запуске.

ТЗ требует, чтобы после перезапуска сервис сам нашёл операции в
PROCESSING и продолжил отправку с прежним ``Idempotency-Key``. Искать их
по таблице операций не нужно: незавершённая отправка — это ровно строка
в ``event_outbox``, которая не дошла до DONE. Outbox и есть список дел.

Разбираем три наследства предыдущего процесса:

* IN_FLIGHT — попытка оборвалась на полпути. Отпускаем сразу, не дожидаясь
  истечения аренды;
* FAILED — попытки были исчерпаны. Перезапуск даёт новый шанс, ключ
  идемпотентности защищает от второго платежа;
* PENDING — работа, до которой просто не дошли руки.

Ни один из случаев не меняет статус операции: она была и остаётся в
PROCESSING до квитанции.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EventType, OperationStatus
from app.repositories import events, intents

log = structlog.get_logger(__name__)


async def resume_unfinished(session: AsyncSession) -> int:
    """Возвращает в очередь всё незавершённое. Отдаёт число операций."""
    released = await intents.release_all_in_flight(session)
    revived = await intents.revive_failed(session)

    resumable = await intents.list_resumable(session)
    for intent in resumable:
        await events.append_event(
            session,
            operation_id=intent.operation_id,
            event_type=EventType.RECOVERY_RESUMED,
            from_status=OperationStatus.PROCESSING,
            to_status=OperationStatus.PROCESSING,
            message=(
                f"Resumed after restart with idempotency key "
                f"'{intent.idempotency_key}' after {intent.attempts} attempt(s)"
            ),
        )

    log.info(
        "recovery.completed",
        released_in_flight=released,
        revived_failed=revived,
        resumed=len(resumable),
        operations=[intent.operation_id for intent in resumable],
    )
    return len(resumable)
