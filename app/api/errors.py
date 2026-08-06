"""Отображение доменных ошибок в коды HTTP.

Маршруты не знают о кодах ответов: они бросают доменную ошибку, а
соответствие «ошибка -> статус» задано здесь в одном месте.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.errors import (
    DomainError,
    DuplicateOperation,
    OperationNotFound,
    ProviderPaymentMismatch,
)

_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    OperationNotFound: status.HTTP_404_NOT_FOUND,
    DuplicateOperation: status.HTTP_409_CONFLICT,
    ProviderPaymentMismatch: status.HTTP_409_CONFLICT,
}


async def _domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    http_status = _STATUS_BY_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST)
    return JSONResponse(
        status_code=http_status,
        content={"code": exc.code, "message": exc.message},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
