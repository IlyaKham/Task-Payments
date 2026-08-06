"""Маршруты операций."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.schemas import (
    CreateOperationRequest,
    ErrorResponse,
    EventResponse,
    OperationResponse,
)
from app.deps import SessionDep
from app.repositories import events as events_repo
from app.repositories import operations as operations_repo
from app.services import operation_service

router = APIRouter(prefix="/operations", tags=["operations"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=OperationResponse,
    responses={status.HTTP_409_CONFLICT: {"model": ErrorResponse}},
)
async def create_operation(
    request: CreateOperationRequest, session: SessionDep
) -> OperationResponse:
    operation = await operation_service.create_operation(session, request)
    return OperationResponse.model_validate(operation)


@router.post(
    "/{operation_id}/submit",
    response_model=OperationResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def submit_operation(
    operation_id: str, response: Response, session: SessionDep
) -> OperationResponse:
    """202 — намерение создано этим запросом, 200 — оно уже существовало."""
    operation, intent_created = await operation_service.submit_operation(
        session, operation_id
    )
    response.status_code = (
        status.HTTP_202_ACCEPTED if intent_created else status.HTTP_200_OK
    )
    return OperationResponse.model_validate(operation)


@router.get(
    "/{operation_id}",
    status_code=status.HTTP_200_OK,
    response_model=OperationResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_operation(operation_id: str, session: SessionDep) -> OperationResponse:
    operation = await operations_repo.get_or_raise(session, operation_id)
    return OperationResponse.model_validate(operation)


@router.get(
    "/{operation_id}/events",
    status_code=status.HTTP_200_OK,
    response_model=list[EventResponse],
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_operation_events(
    operation_id: str, session: SessionDep
) -> list[EventResponse]:
    """История в порядке фиксации. Несуществующая операция — 404, а не пустой список."""
    await operations_repo.get_or_raise(session, operation_id)
    history = await events_repo.list_events(session, operation_id)
    return [EventResponse.model_validate(event) for event in history]
