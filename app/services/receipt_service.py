"""Обработка callback-квитанции — единственного источника финального статуса.

Вся логика выполняется одной транзакцией, как требует ТЗ. Строка операции
берётся с блокировкой FOR UPDATE: транзакция короткая и в сеть не ходит,
поэтому удержание блокировки здесь безопасно и избавляет от гонок между
квитанцией и записью ответа провайдера.

Разбор случаев:

* провайдер ещё не привязан      -> привязываем из квитанции, применяем;
* идентификатор совпал, PROCESSING -> применяем;
* точно такая квитанция уже была -> дедупликация, второго перехода нет;
* операция уже финальна, результат противоположный -> фиксируем как
  проигнорированную, статус не трогаем;
* идентификатор не совпал        -> 409.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ReceiptRequest
from app.domain.enums import (
    RECEIPT_RESULT_TO_STATUS,
    STATUS_TO_EVENT_TYPE,
    EventType,
    OperationStatus,
)
from app.domain.errors import OperationNotFound, ProviderPaymentMismatch
from app.repositories import events, intents, operations, receipts


class ReceiptOutcome(StrEnum):
    """Итог обработки. Все варианты отвечают 204, различаются лишь журналом."""

    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    IGNORED = "IGNORED"


async def process_receipt(session: AsyncSession, request: ReceiptRequest) -> ReceiptOutcome:
    operation = await operations.get_for_update(session, request.operation_id)
    if operation is None:
        raise OperationNotFound(request.operation_id)

    # Связь установлена и не совпадает — квитанция относится к другому платежу.
    if (
        operation.provider_payment_id is not None
        and operation.provider_payment_id != request.provider_payment_id
    ):
        raise ProviderPaymentMismatch(
            operation.operation_id,
            expected=operation.provider_payment_id,
            received=request.provider_payment_id,
        )

    target_status = RECEIPT_RESULT_TO_STATUS[request.result]
    can_apply = operation.status == OperationStatus.PROCESSING

    if can_apply:
        ignore_reason = None
    elif operation.status in (OperationStatus.COMPLETED, OperationStatus.REJECTED):
        ignore_reason = "already_final"
    else:
        # Квитанция на операцию, отправку которой мы не запрашивали.
        ignore_reason = "not_submitted"

    # Дедупликация: если такая квитанция уже сохранена, INSERT не вернёт id.
    # Значит второй переход в то же состояние фиксировать нельзя.
    receipt_id = await receipts.try_record(
        session,
        operation_id=operation.operation_id,
        provider_payment_id=request.provider_payment_id,
        result=request.result,
        message=request.message,
        occurred_at=request.occurred_at,
        applied=can_apply,
        ignore_reason=ignore_reason,
    )
    if receipt_id is None:
        return ReceiptOutcome.DUPLICATE

    if not can_apply:
        # Сюда попадает только квитанция с другим результатом: одинаковую
        # отсеяла дедупликация выше. Фиксируем факт, статус не меняем.
        await events.append_event(
            session,
            operation_id=operation.operation_id,
            event_type=EventType.RECEIPT_CONFLICT_IGNORED,
            from_status=operation.status,
            to_status=operation.status,
            message=(
                f"Receipt {request.result} ignored: operation is {operation.status} "
                f"({ignore_reason})"
            ),
        )
        return ReceiptOutcome.IGNORED

    # Квитанция могла обогнать ответ провайдера — тогда связь устанавливаем здесь.
    if operation.provider_payment_id is None:
        await operations.link_provider_payment(
            session, operation.operation_id, request.provider_payment_id
        )

    await operations.try_finalize(session, operation.operation_id, target_status)

    await events.append_event(
        session,
        operation_id=operation.operation_id,
        event_type=STATUS_TO_EVENT_TYPE[target_status],
        from_status=OperationStatus.PROCESSING,
        to_status=target_status,
        message=request.message or f"Provider reported {request.result}",
    )

    # Исход известен — повторять вызов провайдера больше незачем.
    await intents.mark_done(session, operation.operation_id)

    return ReceiptOutcome.APPLIED
