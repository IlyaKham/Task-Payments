"""Хелперы тестов.

Каждый вызов открывает собственную транзакцию через ``session_scope`` —
ровно как обработчик HTTP-запроса. Иначе тест проверял бы поведение,
которого в бою не существует.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.api.schemas import CreateOperationRequest, ReceiptRequest
from app.db.engine import session_scope
from app.domain.enums import ReceiptResult
from app.domain.models import Operation, SubmitIntent
from app.repositories import events, intents, operations
from app.services import operation_service, receipt_service


async def create_operation(operation_id: str, amount: str = "1000.00") -> None:
    request = CreateOperationRequest.model_validate(
        {
            "operationId": operation_id,
            "amount": amount,
            "currency": "RUB",
            "description": "Оплата заказа",
        }
    )
    async with session_scope() as session:
        await operation_service.create_operation(session, request)


async def submit(operation_id: str) -> bool:
    """Возвращает признак «намерение создано этим вызовом» (202 против 200)."""
    async with session_scope() as session:
        _, created = await operation_service.submit_operation(session, operation_id)
        return created


async def deliver_receipt(
    operation_id: str,
    provider_payment_id: str,
    result: ReceiptResult = ReceiptResult.COMPLETED,
    message: str | None = None,
) -> receipt_service.ReceiptOutcome:
    request = ReceiptRequest.model_validate(
        {
            "providerPaymentId": provider_payment_id,
            "operationId": operation_id,
            "result": result.value,
            "message": message or f"Payment {result.value.lower()}",
            "occurredAt": datetime.now(UTC).isoformat(),
        }
    )
    async with session_scope() as session:
        return await receipt_service.process_receipt(session, request)


async def fetch_operation(operation_id: str) -> Operation:
    async with session_scope() as session:
        return await operations.get_or_raise(session, operation_id)


async def fetch_intent(operation_id: str) -> SubmitIntent | None:
    async with session_scope() as session:
        return await intents.get(session, operation_id)


async def event_types(operation_id: str) -> list[str]:
    async with session_scope() as session:
        history = await events.list_events(session, operation_id)
        return [str(event.type) for event in history]


async def event_ids(operation_id: str) -> list[int]:
    async with session_scope() as session:
        history = await events.list_events(session, operation_id)
        return [event.event_id for event in history]
