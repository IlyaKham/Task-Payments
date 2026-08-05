"""Ошибки предметной области, отображаются в коды HTTP в app/api/errors.py."""

from __future__ import annotations


class DomainError(Exception):
    """Базовый класс для ожидаемых ошибок, не являющихся дефектами кода."""

    code: str = "domain_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OperationNotFound(DomainError):
    code = "operation_not_found"

    def __init__(self, operation_id: str) -> None:
        super().__init__(f"Операция '{operation_id}' не найдена")
        self.operation_id = operation_id


class DuplicateOperation(DomainError):
    code = "duplicate_operation"

    def __init__(self, operation_id: str) -> None:
        super().__init__(f"Операция '{operation_id}' уже существует")
        self.operation_id = operation_id


class ProviderPaymentMismatch(DomainError):
    """Квитанция ссылается на другой платёж, не на связанный с операцией."""

    code = "provider_payment_mismatch"

    def __init__(self, operation_id: str, expected: str, received: str) -> None:
        super().__init__(
            f"Операция '{operation_id}' связана с платежом провайдера "
            f"'{expected}', а в квитанции указан '{received}'"
        )
        self.operation_id = operation_id
        self.expected = expected
        self.received = received


class IllegalStateTransition(DomainError):
    code = "illegal_state_transition"

    def __init__(self, operation_id: str, from_status: str, to_status: str) -> None:
        super().__init__(
            f"Операция '{operation_id}' не может перейти из {from_status} в {to_status}"
        )
        self.operation_id = operation_id
        self.from_status = from_status
        self.to_status = to_status


class ProviderError(Exception):
    """Базовый класс для транспортных сбоев при обращении к провайдеру."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class ProviderUnavailable(ProviderError):
    """Сетевой сбой, таймаут или 5xx: платёж мог быть создан, а мог и нет."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ProviderRejectedRequest(ProviderError):
    """Неповторяемая ошибка 4xx: некорректен сам запрос."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message, retryable=False)
        self.status_code = status_code
