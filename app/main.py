"""Точка входа приложения."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response

from app.api.errors import register_exception_handlers
from app.api.router import api_router
from app.config import get_settings
from app.db.engine import dispose_engine, session_scope
from app.logging import bind_correlation_id, clear_context, configure_logging
from app.provider.http_client import HttpProviderClient
from app.services import recovery
from app.services.dispatcher import Dispatcher

log = structlog.get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"

# Контейнер приложения обычно стартует раньше, чем база начинает принимать
# соединения. Восстановление ждёт её, но не бесконечно.
_RECOVERY_ATTEMPTS = 15
_RECOVERY_RETRY_DELAY = 2.0


async def _resume_unfinished() -> None:
    """Возобновляет незавершённые отправки, переживая недоступность базы.

    Неудача восстановления не повод падать: намерения никуда не делись, и
    диспетчер всё равно подберёт их по истечении аренды.
    """
    for attempt in range(1, _RECOVERY_ATTEMPTS + 1):
        try:
            async with session_scope() as session:
                await recovery.resume_unfinished(session)
        except Exception:
            if attempt == _RECOVERY_ATTEMPTS:
                log.error("recovery.giving_up", attempts=attempt, exc_info=True)
                return
            log.warning("recovery.retrying", attempt=attempt)
            await asyncio.sleep(_RECOVERY_RETRY_DELAY)
        else:
            return


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info("service.starting", provider_url=settings.provider_url)

    provider = HttpProviderClient(
        settings.provider_url,
        connect_timeout=settings.provider_connect_timeout,
        read_timeout=settings.provider_read_timeout,
    )
    dispatcher = Dispatcher(provider=provider, settings=settings)
    app.state.provider = provider
    app.state.dispatcher = dispatcher

    await _resume_unfinished()

    if settings.dispatcher_enabled:
        await dispatcher.start()
    else:
        # Только для тестов, где отправкой управляют вручную.
        log.warning("dispatcher.disabled")

    try:
        yield
    finally:
        # Порядок обратный запуску: сначала перестаём брать работу, потом
        # закрываем соединения. Иначе остановка диспетчера уронит попытки,
        # которые могли бы успеть завершиться.
        await dispatcher.stop()
        await provider.aclose()
        await dispose_engine()
        log.info("service.stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title="Task Payments",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def correlation_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Сквозной идентификатор запроса во всех записях журнала."""
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        clear_context()
        bind_correlation_id(correlation_id)
        try:
            response = await call_next(request)
        finally:
            clear_context()
        response.headers[CORRELATION_HEADER] = correlation_id
        return response

    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
