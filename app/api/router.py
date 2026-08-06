"""Сборка всех маршрутов сервиса."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health, operations, receipts

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(operations.router)
api_router.include_router(receipts.router)
