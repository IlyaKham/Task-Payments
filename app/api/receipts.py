"""Приём callback-квитанций от провайдера."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Response, status

from app.api.schemas import ErrorResponse, ReceiptRequest
from app.deps import SessionDep
from app.services import receipt_service

router = APIRouter(tags=["receipts"])
log = structlog.get_logger(__name__)


@router.post(
    "/receipts",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
async def receive_receipt(request: ReceiptRequest, session: SessionDep) -> Response:
    """Всегда 204, кроме неизвестной операции (404) и чужого платежа (409).

    Повторная и запоздалая конфликтующая квитанции тоже дают 204: провайдер
    не обязан их различать, различие фиксируется у нас в журнале.
    """
    outcome = await receipt_service.process_receipt(session, request)

    log.info(
        "receipt.processed",
        operation_id=request.operation_id,
        provider_payment_id=request.provider_payment_id,
        result=str(request.result),
        outcome=str(outcome),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
