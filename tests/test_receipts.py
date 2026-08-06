"""Обработка квитанций: пункты 4 и 6 автопроверки.

Квитанция — единственный источник финального статуса, поэтому здесь же
проверяется, что ничто другое статус не двигает.
"""

from __future__ import annotations

import pytest

from app.domain.enums import EventType, IntentState, OperationStatus, ReceiptResult
from app.domain.errors import ProviderPaymentMismatch
from app.services.receipt_service import ReceiptOutcome
from tests import helpers

pytestmark = pytest.mark.usefixtures("clean_database")


async def _submitted(operation_id: str = "op-1") -> str:
    await helpers.create_operation(operation_id)
    await helpers.submit(operation_id)
    return operation_id


async def test_receipt_completes_operation() -> None:
    await _submitted()

    outcome = await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.COMPLETED)

    assert outcome == ReceiptOutcome.APPLIED
    operation = await helpers.fetch_operation("op-1")
    assert operation.status == OperationStatus.COMPLETED
    assert operation.provider_payment_id == "pp-1"
    assert await helpers.event_types("op-1") == [
        EventType.CREATED,
        EventType.SUBMIT_REQUESTED,
        EventType.COMPLETED,
    ]


async def test_receipt_rejects_operation() -> None:
    await _submitted()

    outcome = await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.REJECTED)

    assert outcome == ReceiptOutcome.APPLIED
    assert (await helpers.fetch_operation("op-1")).status == OperationStatus.REJECTED


async def test_receipt_before_provider_response_links_payment() -> None:
    """Квитанция обогнала ответ провайдера — связь ставится из неё."""
    await _submitted()
    assert (await helpers.fetch_operation("op-1")).provider_payment_id is None

    await helpers.deliver_receipt("op-1", "pp-early", ReceiptResult.COMPLETED)

    operation = await helpers.fetch_operation("op-1")
    assert operation.provider_payment_id == "pp-early"
    assert operation.status == OperationStatus.COMPLETED


async def test_receipt_closes_outbox_entry() -> None:
    """Исход известен — диспетчеру больше нечего доставлять."""
    await _submitted()
    await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.COMPLETED)

    intent = await helpers.fetch_intent("op-1")
    assert intent is not None
    assert intent.state == IntentState.DONE


async def test_duplicate_receipt_does_not_repeat_transition() -> None:
    """Повторная квитанция: 204 и никакого второго перехода."""
    await _submitted()

    first = await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.COMPLETED)
    second = await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.COMPLETED)
    third = await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.COMPLETED)

    assert first == ReceiptOutcome.APPLIED
    assert second == ReceiptOutcome.DUPLICATE
    assert third == ReceiptOutcome.DUPLICATE

    history = await helpers.event_types("op-1")
    assert history.count(EventType.COMPLETED) == 1
    assert (await helpers.fetch_operation("op-1")).status == OperationStatus.COMPLETED


async def test_late_conflicting_receipt_does_not_flip_result() -> None:
    """Запоздалая противоположная квитанция фиксируется, но не меняет исход."""
    await _submitted()
    await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.COMPLETED)

    outcome = await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.REJECTED)

    assert outcome == ReceiptOutcome.IGNORED
    assert (await helpers.fetch_operation("op-1")).status == OperationStatus.COMPLETED

    history = await helpers.event_types("op-1")
    assert history.count(EventType.RECEIPT_CONFLICT_IGNORED) == 1
    assert EventType.REJECTED not in history


async def test_receipt_for_foreign_payment_is_conflict() -> None:
    """Связь установлена — квитанция чужого платежа отвергается."""
    await _submitted()
    await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.COMPLETED)

    with pytest.raises(ProviderPaymentMismatch):
        await helpers.deliver_receipt("op-1", "pp-other", ReceiptResult.COMPLETED)

    assert (await helpers.fetch_operation("op-1")).provider_payment_id == "pp-1"


async def test_receipt_for_unknown_operation() -> None:
    from app.domain.errors import OperationNotFound

    with pytest.raises(OperationNotFound):
        await helpers.deliver_receipt("op-missing", "pp-1")
