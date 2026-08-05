"""Явный конечный автомат операции.

Здесь перечислены все допустимые переходы. Всё, чего в списке нет, —
дефект кода, а не бизнес-случай. Финальные статусы поглощающие: ни
квитанция, ни запоздалый ответ провайдера, ни восстановление после
перезапуска не могут вывести операцию из COMPLETED или REJECTED.
"""

from __future__ import annotations

from app.domain.enums import TERMINAL_STATUSES, OperationStatus

ALLOWED_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.CREATED: frozenset({OperationStatus.PROCESSING}),
    OperationStatus.PROCESSING: frozenset(
        {OperationStatus.COMPLETED, OperationStatus.REJECTED}
    ),
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.REJECTED: frozenset(),
}


def is_terminal(status: OperationStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(from_status: OperationStatus, to_status: OperationStatus) -> bool:
    return to_status in ALLOWED_TRANSITIONS[from_status]
