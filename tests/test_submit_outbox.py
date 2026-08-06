"""Надёжная запись намерения: пункт 2 автопроверки.

Проверяется главное свойство outbox: сколько бы submit ни пришло —
последовательно или разом, — намерение появляется ровно одно.
"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.enums import EventType, IntentState, OperationStatus
from app.domain.errors import DuplicateOperation, OperationNotFound
from tests import helpers

pytestmark = pytest.mark.usefixtures("clean_database")


async def test_created_operation_has_no_intent() -> None:
    """Пока отправку не запросили, работы для диспетчера нет."""
    await helpers.create_operation("op-1")

    operation = await helpers.fetch_operation("op-1")
    assert operation.status == OperationStatus.CREATED
    assert operation.provider_payment_id is None
    assert await helpers.fetch_intent("op-1") is None
    assert await helpers.event_types("op-1") == [EventType.CREATED]


async def test_duplicate_create_is_rejected() -> None:
    await helpers.create_operation("op-1")
    with pytest.raises(DuplicateOperation):
        await helpers.create_operation("op-1")


async def test_submit_unknown_operation() -> None:
    with pytest.raises(OperationNotFound):
        await helpers.submit("op-missing")


async def test_submit_stores_intent_and_moves_to_processing() -> None:
    await helpers.create_operation("op-1")

    assert await helpers.submit("op-1") is True

    operation = await helpers.fetch_operation("op-1")
    assert operation.status == OperationStatus.PROCESSING

    intent = await helpers.fetch_intent("op-1")
    assert intent is not None
    assert intent.state == IntentState.PENDING
    assert intent.attempts == 0
    # Ключ идемпотентности равен operationId — требование контракта.
    assert intent.idempotency_key == "op-1"
    assert intent.request_body == (
        '{"operationId":"op-1","amount":"1000.00","currency":"RUB"}'
    )

    assert await helpers.event_types("op-1") == [
        EventType.CREATED,
        EventType.SUBMIT_REQUESTED,
    ]


async def test_repeated_submit_does_not_create_second_intent() -> None:
    await helpers.create_operation("op-1")

    assert await helpers.submit("op-1") is True
    assert await helpers.submit("op-1") is False
    assert await helpers.submit("op-1") is False

    # Повторный submit не пишет событий: состояние он не менял.
    assert await helpers.event_types("op-1") == [
        EventType.CREATED,
        EventType.SUBMIT_REQUESTED,
    ]


async def test_concurrent_submits_produce_exactly_one_intent() -> None:
    """Серия одновременных submit одной операции — пункт 2 автопроверки."""
    await helpers.create_operation("op-1")

    results = await asyncio.gather(*(helpers.submit("op-1") for _ in range(25)))

    # Ровно один запрос получает 202, остальные — 200 с текущим состоянием.
    assert sum(results) == 1

    intent = await helpers.fetch_intent("op-1")
    assert intent is not None
    assert intent.state == IntentState.PENDING

    history = await helpers.event_types("op-1")
    assert history.count(EventType.SUBMIT_REQUESTED) == 1
    assert (await helpers.fetch_operation("op-1")).status == OperationStatus.PROCESSING


async def test_event_ids_are_monotonic_within_operation() -> None:
    """eventId растёт в пределах операции и стартует с 1 у каждой."""
    await helpers.create_operation("op-1")
    await helpers.create_operation("op-2")
    await helpers.submit("op-1")
    await helpers.submit("op-2")

    assert await helpers.event_ids("op-1") == [1, 2]
    assert await helpers.event_ids("op-2") == [1, 2]
