"""Проверка готовности."""

from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import text

from app.api.schemas import HealthResponse
from app.deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/health", status_code=status.HTTP_200_OK, response_model=HealthResponse)
async def health(session: SessionDep) -> HealthResponse:
    """Готовность включает доступность базы: без неё сервис бесполезен."""
    await session.execute(text("SELECT 1"))
    return HealthResponse(status="ok", database="ok")
