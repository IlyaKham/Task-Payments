"""Начальная схема: operations, event_outbox, events, receipts

Revision ID: 0001
Revises:
Create Date: 2026-08-05

Колонки со статусами, состояниями и типами — обычный VARCHAR, а не native
ENUM PostgreSQL. Значения проверяются в Python доменными перечислениями;
благодаря этому добавление нового типа события — обычный деплой, а не
миграция с ALTER TYPE.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Имя специально короткое: конвенция именования
        # ("ck_%(table_name)s_%(constraint_name)s") развернёт его в
        # ck_operations_amount_positive. Полное имя получило бы префикс дважды.
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.PrimaryKeyConstraint("operation_id", name="pk_operations"),
        sa.UniqueConstraint("provider_payment_id", name="uq_operations_provider_payment_id"),
    )
    op.create_index("ix_operations_status", "operations", ["status"])

    op.create_table(
        "event_outbox",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_body", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_event_outbox_operation_id_operations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_event_outbox"),
    )
    # Обслуживает опрос диспетчера: «дай работу, которой пора, подешевле».
    op.create_index("ix_event_outbox_due", "event_outbox", ["state", "next_attempt_at"])

    op.create_table(
        "events",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_events_operation_id_operations",
            ondelete="CASCADE",
        ),
        # Составной первичный ключ заодно гарантирует уникальность eventId
        # внутри операции при конкурентных вставках.
        sa.PrimaryKeyConstraint("operation_id", "event_id", name="pk_events"),
    )

    op.create_table(
        "receipts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ignore_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_receipts_operation_id_operations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_receipts"),
        # Механизм дедупликации повторных квитанций.
        sa.UniqueConstraint(
            "operation_id",
            "provider_payment_id",
            "result",
            name="uq_receipts_operation_payment_result",
        ),
    )
    op.create_index("ix_receipts_operation_id", "receipts", ["operation_id"])


def downgrade() -> None:
    op.drop_index("ix_receipts_operation_id", table_name="receipts")
    op.drop_table("receipts")
    op.drop_table("events")
    op.drop_index("ix_event_outbox_due", table_name="event_outbox")
    op.drop_table("event_outbox")
    op.drop_index("ix_operations_status", table_name="operations")
    op.drop_table("operations")
