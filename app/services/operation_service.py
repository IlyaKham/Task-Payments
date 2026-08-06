"""Создание операции и надёжное планирование отправки.

Ключевой момент всего задания находится в ``submit_operation``: переход в
PROCESSING, запись намерения и событие фиксируются одной транзакцией, и
только после её коммита кто-либо может пойти в сеть. Если процесс умрёт
сразу после коммита, намерение останется в базе и будет подхвачено при
следующем запуске.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import CreateOperationRequest, format_amount
from app.domain.enums import EventType, OperationStatus
from app.domain.errors import DuplicateOperation
from app.domain.models import Operation
from app.repositories import events, intents, operations


def build_provider_request_body(operation: Operation) -> str:
    """Собирает тело запроса к провайдеру.

    Вызывается ровно один раз — при создании намерения. Результат ложится
    в базу и дальше переотправляется дословно: контракт требует неизменное
    тело при том же ключе идемпотентности.
    """
    payload = {
        "operationId": operation.operation_id,
        "amount": format_amount(operation.amount),
        "currency": operation.currency,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def create_operation(
    session: AsyncSession, request: CreateOperationRequest
) -> Operation:
    """Создаёт операцию в статусе CREATED."""
    operation = await operations.create(
        session,
        operation_id=request.operation_id,
        amount=request.amount,
        currency=request.currency,
        description=request.description,
    )
    if operation is None:
        raise DuplicateOperation(request.operation_id)

    await events.append_event(
        session,
        operation_id=operation.operation_id,
        event_type=EventType.CREATED,
        from_status=None,
        to_status=OperationStatus.CREATED,
        message="Operation created",
    )
    return operation


async def submit_operation(
    session: AsyncSession, operation_id: str
) -> tuple[Operation, bool]:
    """Планирует отправку. Возвращает операцию и признак «намерение создано».

    Признак определяет код ответа: 202 у единственного запроса, выигравшего
    гонку, и 200 у всех повторных и конкурентных.
    """
    operation = await operations.get_or_raise(session, operation_id)

    # Атомарный CAS: ровно один вызов получит True даже при десятке
    # одновременных запросов.
    started = await operations.try_begin_processing(session, operation_id)

    if started:
        await intents.create(
            session,
            operation_id=operation.operation_id,
            idempotency_key=operation.operation_id,
            request_body=build_provider_request_body(operation),
        )
        await events.append_event(
            session,
            operation_id=operation.operation_id,
            event_type=EventType.SUBMIT_REQUESTED,
            from_status=OperationStatus.CREATED,
            to_status=OperationStatus.PROCESSING,
            message="Submit intent stored",
        )
        # UPDATE выполнялся на уровне Core, объект в сессии остался старым.
        await session.refresh(operation)

    return operation, started
