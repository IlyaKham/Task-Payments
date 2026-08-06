"""Доставка до провайдера: пункты 3, 4, 5 и 8 автопроверки.

Поддельный провайдер ведёт себя как симулятор: платёж заводится по ключу
идемпотентности, повтор с тем же ключом отдаёт тот же платёж. Счётчик
``payments_created`` играет роль внутреннего аудита провайдера, по
которому проверяется главный инвариант задания.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update

from app.config import Settings
from app.db.engine import session_scope
from app.domain.enums import EventType, IntentState, OperationStatus, ReceiptResult
from app.domain.errors import ProviderRejectedRequest, ProviderUnavailable
from app.domain.models import SubmitIntent
from app.provider.base import ProviderAcceptance
from app.services import recovery
from app.services.dispatcher import Dispatcher
from tests import helpers

pytestmark = pytest.mark.usefixtures("clean_database")


class FakeProvider:
    """Провайдер с идемпотентностью по ключу и управляемыми сбоями.

    Сценарий задаётся списком исходов: ``"ok"``, ``"lost"`` (платёж принят,
    ответ не дошёл), ``"unavailable"`` (503 или сеть) и ``"fatal"`` (4xx).
    Платёж регистрируется до разбора исхода — именно так и теряется ответ
    на фактически принятый платёж.
    """

    def __init__(self, *script: str, default: str = "ok") -> None:
        self.calls: list[dict[str, str]] = []
        self._script = list(script)
        self._default = default
        self._payments: dict[str, str] = {}

    @property
    def payments_created(self) -> int:
        return len(self._payments)

    @property
    def keys_used(self) -> set[str]:
        return {call["idempotency_key"] for call in self.calls}

    async def create_payment(
        self, *, idempotency_key: str, correlation_id: str, body: str
    ) -> ProviderAcceptance:
        self.calls.append(
            {
                "idempotency_key": idempotency_key,
                "correlation_id": correlation_id,
                "body": body,
            }
        )
        payment_id = self._payments.setdefault(
            idempotency_key, f"pp-{len(self._payments) + 1}"
        )

        outcome = self._script.pop(0) if self._script else self._default
        if outcome == "lost":
            raise ProviderUnavailable("платёж принят, ответ не дошёл")
        if outcome == "unavailable":
            raise ProviderUnavailable("503 Service Unavailable")
        if outcome == "fatal":
            raise ProviderRejectedRequest("некорректный запрос", status_code=400)
        return ProviderAcceptance(provider_payment_id=payment_id, status="ACCEPTED")

    async def aclose(self) -> None:
        return None


def _dispatcher(provider: object, settings: Settings) -> Dispatcher:
    return Dispatcher(provider=provider, settings=settings)  # type: ignore[arg-type]


async def _force_state(operation_id: str, state: IntentState) -> None:
    """Имитация обрыва: строка outbox остаётся в промежуточном состоянии."""
    async with session_scope() as session:
        await session.execute(
            update(SubmitIntent)
            .where(SubmitIntent.operation_id == operation_id)
            .values(state=state)
        )


async def _submitted(operation_id: str = "op-1") -> str:
    await helpers.create_operation(operation_id)
    await helpers.submit(operation_id)
    return operation_id


async def test_delivery_does_not_finalise_operation(fast_settings: Settings) -> None:
    """Транспортный успех — это не результат платежа."""
    await _submitted()
    provider = FakeProvider()

    assert await _dispatcher(provider, fast_settings).run_once() == 1

    operation = await helpers.fetch_operation("op-1")
    assert operation.status == OperationStatus.PROCESSING
    assert operation.provider_payment_id == "pp-1"

    intent = await helpers.fetch_intent("op-1")
    assert intent is not None and intent.state == IntentState.DONE

    assert await helpers.event_types("op-1") == [
        EventType.CREATED,
        EventType.SUBMIT_REQUESTED,
        EventType.PROVIDER_ACCEPTED,
    ]


async def test_provider_receives_contract_fields(fast_settings: Settings) -> None:
    await _submitted()
    provider = FakeProvider()

    await _dispatcher(provider, fast_settings).run_once()

    call = provider.calls[0]
    assert call["idempotency_key"] == "op-1"
    # ТЗ требует X-Correlation-ID равным operationId, а не сквозному uuid.
    assert call["correlation_id"] == "op-1"
    assert call["body"] == '{"operationId":"op-1","amount":"1000.00","currency":"RUB"}'


async def test_lost_response_never_creates_second_payment(
    fast_settings: Settings,
) -> None:
    """Пункты 3 и 8: потеря ответа после фактического принятия платежа."""
    await _submitted()
    provider = FakeProvider("lost", "unavailable", "ok")
    dispatcher = _dispatcher(provider, fast_settings)

    for _ in range(3):
        await dispatcher.run_once()

    assert len(provider.calls) == 3
    # Внутренний аудит провайдера: платёж ровно один.
    assert provider.payments_created == 1
    # Все повторы ушли с тем же ключом и тем же телом.
    assert provider.keys_used == {"op-1"}
    assert len({call["body"] for call in provider.calls}) == 1

    operation = await helpers.fetch_operation("op-1")
    assert operation.status == OperationStatus.PROCESSING
    assert operation.provider_payment_id == "pp-1"

    history = await helpers.event_types("op-1")
    assert history.count(EventType.PROVIDER_ATTEMPT_FAILED) == 2
    assert history.count(EventType.PROVIDER_ACCEPTED) == 1


async def test_exhausted_attempts_do_not_reject_operation(
    fast_settings: Settings,
) -> None:
    """Провайдер молчит — это не отказ платежа. Статус имеет право менять
    только квитанция."""
    await _submitted()
    provider = FakeProvider(default="unavailable")
    dispatcher = _dispatcher(provider, fast_settings)

    for _ in range(fast_settings.provider_max_attempts + 2):
        await dispatcher.run_once()

    assert len(provider.calls) == fast_settings.provider_max_attempts

    intent = await helpers.fetch_intent("op-1")
    assert intent is not None
    assert intent.state == IntentState.FAILED
    assert (await helpers.fetch_operation("op-1")).status == OperationStatus.PROCESSING


async def test_fatal_error_stops_retries_without_rejecting(
    fast_settings: Settings,
) -> None:
    await _submitted()
    provider = FakeProvider(default="fatal")
    dispatcher = _dispatcher(provider, fast_settings)

    await dispatcher.run_once()
    await dispatcher.run_once()

    # Неповторяемая ошибка: второй попытки не было.
    assert len(provider.calls) == 1
    intent = await helpers.fetch_intent("op-1")
    assert intent is not None and intent.state == IntentState.FAILED
    assert (await helpers.fetch_operation("op-1")).status == OperationStatus.PROCESSING


async def test_receipt_arriving_during_provider_call(fast_settings: Settings) -> None:
    """Пункт 4: callback приходит раньше ответа провайдера.

    Заодно это проверка того, что на время внешнего вызова не удерживается
    ни одна блокировка операции: иначе обработка квитанции здесь встала бы
    намертво и тест не уложился бы в таймаут.
    """
    await _submitted()

    class ReceiptRacingProvider(FakeProvider):
        async def create_payment(
            self, *, idempotency_key: str, correlation_id: str, body: str
        ) -> ProviderAcceptance:
            payment_id = self._payments.setdefault(idempotency_key, "pp-race")
            self.calls.append(
                {
                    "idempotency_key": idempotency_key,
                    "correlation_id": correlation_id,
                    "body": body,
                }
            )
            # Квитанция обгоняет ответ на этот самый запрос.
            await helpers.deliver_receipt(
                idempotency_key, payment_id, ReceiptResult.COMPLETED
            )
            return ProviderAcceptance(provider_payment_id=payment_id, status="ACCEPTED")

    provider = ReceiptRacingProvider()

    async with asyncio.timeout(30):
        await _dispatcher(provider, fast_settings).run_once()

    operation = await helpers.fetch_operation("op-1")
    # Поздний 202 не имеет права вытащить операцию из финального статуса.
    assert operation.status == OperationStatus.COMPLETED
    assert operation.provider_payment_id == "pp-race"
    assert provider.payments_created == 1

    history = await helpers.event_types("op-1")
    assert history.count(EventType.COMPLETED) == 1
    assert history[-1] == EventType.PROVIDER_ACCEPTED


async def test_recovery_resumes_interrupted_attempt(fast_settings: Settings) -> None:
    """Пункт 5: остановка и повторный запуск во время обработки."""
    await _submitted()
    # Процесс умер, не успев записать результат попытки.
    await _force_state("op-1", IntentState.IN_FLIGHT)

    async with session_scope() as session:
        resumed = await recovery.resume_unfinished(session)
    assert resumed == 1

    intent = await helpers.fetch_intent("op-1")
    assert intent is not None and intent.state == IntentState.PENDING
    assert EventType.RECOVERY_RESUMED in await helpers.event_types("op-1")

    provider = FakeProvider()
    assert await _dispatcher(provider, fast_settings).run_once() == 1

    # Продолжение идёт с прежним ключом идемпотентности.
    assert provider.keys_used == {"op-1"}
    assert provider.payments_created == 1
    intent = await helpers.fetch_intent("op-1")
    assert intent is not None and intent.state == IntentState.DONE


async def test_recovery_skips_finished_operations(fast_settings: Settings) -> None:
    """Исход уже известен — воскрешать намерение незачем."""
    await _submitted()
    await helpers.deliver_receipt("op-1", "pp-1", ReceiptResult.COMPLETED)
    await _force_state("op-1", IntentState.FAILED)

    async with session_scope() as session:
        assert await recovery.resume_unfinished(session) == 0

    intent = await helpers.fetch_intent("op-1")
    assert intent is not None and intent.state == IntentState.FAILED

    provider = FakeProvider()
    assert await _dispatcher(provider, fast_settings).run_once() == 0
    assert provider.calls == []


async def test_batch_processes_many_operations(fast_settings: Settings) -> None:
    """Пачка обрабатывается параллельно, по одному платежу на операцию."""
    ids = [f"op-{index}" for index in range(12)]
    for operation_id in ids:
        await _submitted(operation_id)

    provider = FakeProvider()
    assert await _dispatcher(provider, fast_settings).run_once() == len(ids)

    assert provider.payments_created == len(ids)
    assert provider.keys_used == set(ids)
    for operation_id in ids:
        intent = await helpers.fetch_intent(operation_id)
        assert intent is not None and intent.state == IntentState.DONE
