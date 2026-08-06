"""Перечисления предметной области.

StrEnum даёт одинаковые значения в базе и в JSON, поэтому между
хранилищем и контрактом API не нужен слой преобразования.
"""

from __future__ import annotations

from enum import StrEnum


class OperationStatus(StrEnum):
    """Жизненный цикл платёжной операции (задан техническим заданием)."""

    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class ReceiptResult(StrEnum):
    """Итог, сообщённый в callback-квитанции провайдера."""

    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class IntentState(StrEnum):
    """Состояние надёжно сохранённого намерения отправки (запись outbox)."""

    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    DONE = "DONE"
    FAILED = "FAILED"


class EventType(StrEnum):
    """Записи истории переходов.

    Список намеренно короткий. Повторный submit и повторная квитанция не
    пишут ничего: они не меняют состояние, а десяток одинаковых записей от
    конкурентных запросов только замусорит историю.

    События без смены статуса (``PROVIDER_*``, ``RECOVERY_RESUMED``,
    ``RECEIPT_CONFLICT_IGNORED``) фиксируют факты, но операцию не двигают.
    Так сохраняется инвариант: финальный статус определяет только квитанция.
    """

    CREATED = "CREATED"
    SUBMIT_REQUESTED = "SUBMIT_REQUESTED"
    PROVIDER_ATTEMPT_FAILED = "PROVIDER_ATTEMPT_FAILED"
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"
    RECOVERY_RESUMED = "RECOVERY_RESUMED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    RECEIPT_CONFLICT_IGNORED = "RECEIPT_CONFLICT_IGNORED"


TERMINAL_STATUSES: frozenset[OperationStatus] = frozenset(
    {OperationStatus.COMPLETED, OperationStatus.REJECTED}
)

RECEIPT_RESULT_TO_STATUS: dict[ReceiptResult, OperationStatus] = {
    ReceiptResult.COMPLETED: OperationStatus.COMPLETED,
    ReceiptResult.REJECTED: OperationStatus.REJECTED,
}

STATUS_TO_EVENT_TYPE: dict[OperationStatus, EventType] = {
    OperationStatus.COMPLETED: EventType.COMPLETED,
    OperationStatus.REJECTED: EventType.REJECTED,
}
