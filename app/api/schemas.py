"""DTO внешнего контракта.

Схемы отделены от моделей БД намеренно: контракт API зафиксирован
техническим заданием и не должен меняться из-за правок в схеме хранения.

Сумма принимается и отдаётся строкой. Это не косметика: float для денег
теряет точность, а строка исключает любые неявные преобразования между
JSON, Python и NUMERIC.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.domain.enums import EventType, OperationStatus, ReceiptResult

# До 18 цифр целой части и не более двух знаков после точки.
_AMOUNT_RE = re.compile(r"^\d{1,18}(\.\d{1,2})?$")
_CENTS = Decimal("0.01")


def _parse_amount(value: Any) -> Decimal:
    """Проверяет сумму по правилам ТЗ: положительная, максимум два знака."""
    if isinstance(value, float):
        raise ValueError(
            "сумма должна быть строкой: число с плавающей точкой теряет точность"
        )

    text = str(value).strip()
    if not _AMOUNT_RE.fullmatch(text):
        raise ValueError(
            "сумма должна быть положительным десятичным числом "
            "не более чем с двумя знаками после точки"
        )

    try:
        amount = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - отсечено регуляркой
        raise ValueError("некорректная сумма") from exc

    if amount <= 0:
        raise ValueError("сумма должна быть больше нуля")

    return amount.quantize(_CENTS)


def format_amount(value: Decimal) -> str:
    """Единый формат вывода суммы: ровно два знака после точки."""
    return f"{value.quantize(_CENTS):f}"


def format_timestamp(value: datetime) -> str:
    """ISO 8601 в UTC с суффиксом Z, как в примерах ТЗ."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CamelModel(BaseModel):
    """Внутри — snake_case, наружу — camelCase контракта."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="ignore",
    )


class CreateOperationRequest(CamelModel):
    operation_id: Annotated[str, Field(min_length=1, max_length=128)]
    amount: Decimal
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    description: Annotated[str | None, Field(default=None, max_length=4096)]

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, value: Any) -> Decimal:
        return _parse_amount(value)

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        normalized = value.upper()
        if normalized != "RUB":
            raise ValueError("поддерживается только валюта RUB")
        return normalized

    @field_validator("operation_id")
    @classmethod
    def _validate_operation_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("operationId не может быть пустым")
        return stripped


class OperationResponse(CamelModel):
    operation_id: str
    amount: Decimal
    currency: str
    description: str | None
    status: OperationStatus
    provider_payment_id: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("amount")
    def _serialize_amount(self, value: Decimal) -> str:
        return format_amount(value)

    @field_serializer("created_at", "updated_at")
    def _serialize_timestamps(self, value: datetime) -> str:
        return format_timestamp(value)


class EventResponse(CamelModel):
    event_id: int
    type: EventType
    from_status: OperationStatus | None
    to_status: OperationStatus | None
    message: str | None
    occurred_at: datetime

    @field_serializer("occurred_at")
    def _serialize_occurred_at(self, value: datetime) -> str:
        return format_timestamp(value)


class ReceiptRequest(CamelModel):
    provider_payment_id: Annotated[str, Field(min_length=1, max_length=128)]
    operation_id: Annotated[str, Field(min_length=1, max_length=128)]
    result: ReceiptResult
    message: Annotated[str | None, Field(default=None, max_length=4096)]
    occurred_at: datetime | None = None


class ErrorResponse(BaseModel):
    code: str
    message: str


class HealthResponse(BaseModel):
    status: str
    database: str
