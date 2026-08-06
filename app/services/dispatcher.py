"""Фоновый диспетчер: потребитель ``event_outbox``.

Здесь замыкается транзакционный outbox. Сервис при ``submit`` только
записал намерение — дойти до провайдера обязан этот цикл.

Порядок шагов выбран так, чтобы падение процесса в любой точке не
создавало второй платёж и не теряло работу:

1. захват пачки намерений отдельной транзакцией, которая **коммитится до**
   сетевого вызова: строка помечена IN_FLIGHT и получила аренду;
2. HTTP-вызов уже вне транзакции — ни одна блокировка БД не удерживается
   на время работы с сетью;
3. результат записывается новой короткой транзакцией.

Если процесс умрёт между шагами 2 и 3, платёж у провайдера, возможно, уже
создан, но об этом никто не знает. Аренда истечёт, намерение вернётся в
очередь, и повтор уйдёт с тем же ``Idempotency-Key`` — провайдер вернёт
тот же ``providerPaymentId`` и второго платежа не появится. Именно на этом
держится главный инвариант задания.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from app.config import Settings
from app.db.engine import session_scope
from app.domain.enums import EventType, OperationStatus
from app.domain.errors import ProviderError, ProviderRejectedRequest, ProviderUnavailable
from app.provider.base import ProviderAcceptance, ProviderClient
from app.provider.retry import compute_backoff
from app.repositories import events, intents, operations

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ClaimedWork:
    """Снимок захваченного намерения.

    Обычные значения, а не ORM-объект: сессия захвата закрывается до
    сетевого вызова, и переносить между транзакциями живую сущность
    незачем.
    """

    operation_id: str
    idempotency_key: str
    request_body: str
    attempt: int


class Dispatcher:
    def __init__(self, *, provider: ProviderClient, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._limit = asyncio.Semaphore(settings.dispatcher_concurrency)
        # Намерения, захваченные этим процессом и ещё не закрытые. При
        # остановке всё, что здесь осталось, возвращается в очередь.
        self._claimed: set[str] = set()

    # --- жизненный цикл ---------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="outbox-dispatcher")
        log.info(
            "dispatcher.started",
            poll_interval=self._settings.dispatcher_poll_interval,
            batch_size=self._settings.dispatcher_batch_size,
            concurrency=self._settings.dispatcher_concurrency,
        )

    async def stop(self) -> None:
        """Корректное завершение: даём доработать, затем отпускаем остаток.

        Прерванная попытка не должна ждать истечения аренды — она
        возвращается в PENDING сразу, чтобы следующий запуск продолжил
        работу без паузы.
        """
        if self._task is None:
            return

        self._stopping.set()
        _, pending = await asyncio.wait(
            {self._task}, timeout=self._settings.shutdown_grace_seconds
        )
        if pending:
            log.warning("dispatcher.grace_expired", abandoned=len(self._claimed))
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

        self._task = None
        await self._release_claimed()
        log.info("dispatcher.stopped")

    async def _release_claimed(self) -> None:
        if not self._claimed:
            return
        abandoned = sorted(self._claimed)
        self._claimed.clear()
        try:
            async with session_scope() as session:
                for operation_id in abandoned:
                    await intents.release_for_retry(session, operation_id)
        except Exception:
            # Не беда: аренда истечёт и намерения вернутся в очередь сами.
            log.error("dispatcher.release_failed", operations=abandoned, exc_info=True)
        else:
            log.info("dispatcher.released", operations=abandoned)

    # --- основной цикл ----------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Недоступная база или иной сбой опроса не должны убивать
                # цикл: провайдер и БД поднимутся, работа никуда не делась.
                log.error("dispatcher.tick_failed", exc_info=True)
                processed = 0

            if processed == 0:
                await self._idle()

    async def run_once(self) -> int:
        """Один проход: захватить пачку и довести её до конца.

        Публичный, потому что этим же методом пользуются тесты: гонять
        фоновый цикл со sleep'ами ради проверки — рецепт мигающих тестов.
        """
        work = await self._claim()
        if not work:
            return 0

        await asyncio.gather(
            *(self._process(item) for item in work), return_exceptions=False
        )
        return len(work)

    async def _idle(self) -> None:
        """Пауза до следующего опроса, прерываемая остановкой."""
        with suppress(TimeoutError):
            await asyncio.wait_for(
                self._stopping.wait(), timeout=self._settings.dispatcher_poll_interval
            )

    async def _claim(self) -> list[ClaimedWork]:
        """Забирает пачку работы. Транзакция закрывается до вызова сети."""
        async with session_scope() as session:
            rows = await intents.claim_due(
                session,
                now=_utcnow(),
                limit=self._settings.dispatcher_batch_size,
                lease_seconds=self._settings.intent_lease_seconds,
            )
            work = [
                ClaimedWork(
                    operation_id=row.operation_id,
                    idempotency_key=row.idempotency_key,
                    request_body=row.request_body,
                    attempt=row.attempts,
                )
                for row in rows
            ]

        self._claimed.update(item.operation_id for item in work)
        return work

    # --- обработка одного намерения ---------------------------------------

    async def _process(self, work: ClaimedWork) -> None:
        """Внешняя защита: из обработки одного намерения не должно вылетать
        ничего, кроме отмены.

        Иначе исключение всплыло бы в ``gather`` и осиротило соседние
        задачи пачки — прямо посреди их HTTP-вызовов.
        """
        try:
            await self._process_one(work)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Результат попытки не записан. Намерение осталось IN_FLIGHT и
            # вернётся в очередь по истечении аренды — работа не потеряна.
            log.error(
                "dispatcher.result_not_recorded",
                operation_id=work.operation_id,
                attempt=work.attempt,
                exc_info=True,
            )

    async def _process_one(self, work: ClaimedWork) -> None:
        async with self._limit:
            try:
                acceptance = await self._call_provider(work)
            except ProviderRejectedRequest as exc:
                await self._on_failure(work, exc, retryable=False)
                return
            except ProviderUnavailable as exc:
                await self._on_failure(work, exc, retryable=True)
                return
            except asyncio.CancelledError:
                # Процесс останавливают. Намерение отпустит ``stop``.
                raise
            except Exception as exc:  # pragma: no cover - защита от дефекта
                # Неизвестный сбой трактуем как «исход неизвестен»: это
                # безопасная сторона, повтор идёт с тем же ключом.
                log.error(
                    "provider.attempt_crashed",
                    operation_id=work.operation_id,
                    attempt=work.attempt,
                    exc_info=True,
                )
                await self._on_failure(work, exc, retryable=True)
                return

            await self._on_accepted(work, acceptance)

    async def _call_provider(self, work: ClaimedWork) -> ProviderAcceptance:
        log.info(
            "provider.attempt_started",
            operation_id=work.operation_id,
            attempt=work.attempt,
            idempotency_key=work.idempotency_key,
        )
        # X-Correlation-ID контракт провайдера требует равным operationId —
        # это не тот сквозной идентификатор, что у входящих HTTP-запросов.
        return await self._provider.create_payment(
            idempotency_key=work.idempotency_key,
            correlation_id=work.operation_id,
            body=work.request_body,
        )

    async def _on_accepted(
        self, work: ClaimedWork, acceptance: ProviderAcceptance
    ) -> None:
        """Провайдер принял платёж. Финальный статус этим не ставится."""
        async with session_scope() as session:
            linked = await operations.link_provider_payment(
                session, work.operation_id, acceptance.provider_payment_id
            )
            operation = await operations.get(session, work.operation_id)
            current = operation.status if operation else OperationStatus.PROCESSING

            if not linked and operation is not None:
                if operation.provider_payment_id != acceptance.provider_payment_id:
                    # Единственный признак того, что платежей всё-таки два.
                    # Молча пройти мимо нельзя: это нарушение инварианта.
                    log.error(
                        "provider.payment_id_mismatch",
                        operation_id=work.operation_id,
                        stored_provider_payment_id=operation.provider_payment_id,
                        received_provider_payment_id=acceptance.provider_payment_id,
                        attempt=work.attempt,
                    )

            await events.append_event(
                session,
                operation_id=work.operation_id,
                event_type=EventType.PROVIDER_ACCEPTED,
                from_status=current,
                to_status=current,
                message=(
                    f"Provider accepted payment {acceptance.provider_payment_id} "
                    f"({acceptance.status}) on attempt {work.attempt}"
                ),
            )
            # Исход у провайдера зафиксирован — повторять вызов незачем.
            await intents.mark_done(session, work.operation_id)

        self._claimed.discard(work.operation_id)
        log.info(
            "provider.accepted",
            operation_id=work.operation_id,
            provider_payment_id=acceptance.provider_payment_id,
            provider_status=acceptance.status,
            attempt=work.attempt,
        )

    async def _on_failure(
        self, work: ClaimedWork, error: Exception, *, retryable: bool
    ) -> None:
        """Попытка не удалась. Статус операции не трогаем ни при каком исходе.

        Ни транспортный сбой, ни исчерпание попыток не являются отказом
        платежа: право на финальный статус есть только у квитанции.
        """
        message = getattr(error, "message", None) or str(error)
        attempts_left = retryable and work.attempt < self._settings.provider_max_attempts

        async with session_scope() as session:
            if attempts_left:
                delay = compute_backoff(
                    work.attempt,
                    base=self._settings.provider_backoff_base,
                    maximum=self._settings.provider_backoff_max,
                    jitter=self._settings.provider_backoff_jitter,
                )
                await intents.reschedule(
                    session,
                    work.operation_id,
                    next_attempt_at=_utcnow() + timedelta(seconds=delay),
                    error=message,
                )
            else:
                delay = 0.0
                await intents.mark_failed(session, work.operation_id, message)

            await events.append_event(
                session,
                operation_id=work.operation_id,
                event_type=EventType.PROVIDER_ATTEMPT_FAILED,
                from_status=OperationStatus.PROCESSING,
                to_status=OperationStatus.PROCESSING,
                message=f"Attempt {work.attempt} failed: {message}"[:2000],
            )

        self._claimed.discard(work.operation_id)
        log.warning(
            "provider.attempt_failed",
            operation_id=work.operation_id,
            attempt=work.attempt,
            retryable=isinstance(error, ProviderError) and error.retryable,
            will_retry=attempts_left,
            retry_in=round(delay, 3) if attempts_left else None,
            error=message,
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)
