"""Модели SQLAlchemy.

Четыре таблицы, у каждой одна зона ответственности:

* ``operations``   — текущее состояние, одна строка на operationId;
* ``event_outbox`` — транзакционный outbox: надёжная запись «мы намерены
  вызвать провайдера», которая пишется в той же транзакции, что и переход
  CREATED -> PROCESSING, до любого сетевого вызова;
* ``events``       — история переходов только на добавление, eventId
  монотонен *в пределах одной операции*;
* ``receipts``     — все полученные квитанции: одновременно журнал
  идемпотентности и материал для аудита.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.domain.enums import EventType, IntentState, OperationStatus, ReceiptResult

# Храним как VARCHAR, а не как native ENUM PostgreSQL: добавить значение
# потом — обычная миграция вместо возни с ALTER TYPE.
_OperationStatusType = SAEnum(
    OperationStatus,
    name="operation_status",
    native_enum=False,
    length=16,
    validate_strings=True,
)
_IntentStateType = SAEnum(
    IntentState, name="intent_state", native_enum=False, length=16, validate_strings=True
)
_EventTypeType = SAEnum(
    EventType, name="event_type", native_enum=False, length=32, validate_strings=True
)
_ReceiptResultType = SAEnum(
    ReceiptResult, name="receipt_result", native_enum=False, length=16, validate_strings=True
)


def _utcnow_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Operation(Base):
    __tablename__ = "operations"

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[OperationStatus] = mapped_column(
        _OperationStatusType, nullable=False, default=OperationStatus.CREATED
    )
    # Устанавливается ровно один раз: либо из ответа 202 провайдера, либо из
    # первой валидной квитанции — что придёт раньше. Никогда не перезаписывается.
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = _utcnow_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    intent: Mapped[SubmitIntent | None] = relationship(
        back_populates="operation", uselist=False, lazy="noload"
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint("provider_payment_id", name="uq_operations_provider_payment_id"),
        Index("ix_operations_status", "status"),
    )


class SubmitIntent(Base):
    """Запись outbox. Источник истины о том, что отправка запрошена и
    обязана дойти до провайдера, — существование этой строки, а не HTTP-вызов.
    """

    __tablename__ = "event_outbox"

    operation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("operations.operation_id", ondelete="CASCADE"), primary_key=True
    )
    # Всегда равен operation_id, но хранится явно: значение, уходящее
    # провайдеру, должно быть сохранённым фактом, а не вычисленным на лету.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Точное тело запроса, сериализованное один раз. Каждый повтор шлёт эти
    # же байты дословно, как требует контракт провайдера.
    request_body: Mapped[str] = mapped_column(Text, nullable=False)

    state: Mapped[IntentState] = mapped_column(
        _IntentStateType, nullable=False, default=IntentState.PENDING
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Аренда на время HTTP-вызова. Транзакция захвата коммитится до начала
    # сетевого запроса, поэтому падение процесса обнаруживается по истечении
    # аренды.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = _utcnow_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    operation: Mapped[Operation] = relationship(back_populates="intent", lazy="noload")

    __table_args__ = (
        Index("ix_event_outbox_due", "state", "next_attempt_at"),
    )


class Event(Base):
    """История только на добавление. ``event_id`` монотонен внутри операции."""

    __tablename__ = "events"

    operation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("operations.operation_id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    type: Mapped[EventType] = mapped_column(_EventTypeType, nullable=False)
    from_status: Mapped[OperationStatus | None] = mapped_column(
        _OperationStatusType, nullable=True
    )
    to_status: Mapped[OperationStatus | None] = mapped_column(_OperationStatusType, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = _utcnow_column()

    # Составной первичный ключ (operation_id, event_id) сам даёт индекс,
    # нужный для чтения истории одной операции по порядку.


class Receipt(Base):
    """Журнал полученных квитанций.

    Уникальное ограничение — механизм дедупликации: если
    ``INSERT ... ON CONFLICT DO NOTHING`` ничего не вставил, значит ровно
    такая квитанция уже обработана.
    """

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("operations.operation_id", ondelete="CASCADE"), nullable=False
    )
    provider_payment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[ReceiptResult] = mapped_column(_ReceiptResultType, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # False, если квитанция пришла, когда операция уже была финальной, и
    # поэтому ничего изменить не могла.
    applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    ignore_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    received_at: Mapped[datetime] = _utcnow_column()

    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "provider_payment_id",
            "result",
            name="uq_receipts_operation_payment_result",
        ),
        Index("ix_receipts_operation_id", "operation_id"),
    )
