"""Классификация ответов провайдера.

Самая ценная часть клиента — не сам запрос, а решение «повторять или
нет». Ошибка здесь стоит либо ложного отказа, либо второго платежа.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.domain.errors import ProviderRejectedRequest, ProviderUnavailable
from app.provider.base import ProviderAcceptance
from app.provider.http_client import HttpProviderClient

BODY = '{"operationId":"operation-123","amount":"1000.00","currency":"RUB"}'


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpProviderClient:
    client = HttpProviderClient(
        "http://provider-simulator:8081", connect_timeout=1.0, read_timeout=1.0
    )
    # Транспорт подменяется уже после сборки: так проверяется настоящий
    # клиент со своими заголовками и таймаутами, а не его двойник.
    client._client = httpx.AsyncClient(
        base_url="http://provider-simulator:8081",
        transport=httpx.MockTransport(handler),
    )
    return client


async def _call(handler: Callable[[httpx.Request], httpx.Response]) -> ProviderAcceptance:
    client = _client(handler)
    try:
        return await client.create_payment(
            idempotency_key="operation-123",
            correlation_id="operation-123",
            body=BODY,
        )
    finally:
        await client.aclose()


def _accepted(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        202, json={"providerPaymentId": "aa5b7856", "status": "ACCEPTED"}
    )


async def test_accepted_response_is_parsed() -> None:
    acceptance = await _call(_accepted)
    assert acceptance.provider_payment_id == "aa5b7856"
    assert acceptance.status == "ACCEPTED"


async def test_request_matches_provider_contract() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["idempotency_key"] = request.headers.get("Idempotency-Key")
        seen["correlation_id"] = request.headers.get("X-Correlation-ID")
        seen["content"] = request.content
        return _accepted(request)

    await _call(handler)

    assert seen["method"] == "POST"
    assert seen["url"] == "http://provider-simulator:8081/payments"
    assert seen["idempotency_key"] == "operation-123"
    assert seen["correlation_id"] == "operation-123"
    # Тело уходит теми же байтами, что сохранены в outbox.
    assert seen["content"] == BODY.encode("utf-8")


@pytest.mark.parametrize("code", [500, 502, 503, 504, 429, 408])
async def test_retryable_statuses(code: int) -> None:
    with pytest.raises(ProviderUnavailable):
        await _call(lambda _: httpx.Response(code, text="temporary"))


@pytest.mark.parametrize("code", [400, 401, 404, 422])
async def test_fatal_statuses(code: int) -> None:
    with pytest.raises(ProviderRejectedRequest) as info:
        await _call(lambda _: httpx.Response(code, text="bad request"))
    assert info.value.retryable is False
    assert info.value.status_code == code


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(202, text="<html>not json</html>"),
        httpx.Response(202, json={"status": "ACCEPTED"}),
        httpx.Response(202, json={"providerPaymentId": ""}),
        httpx.Response(202, json=["not", "an", "object"]),
    ],
    ids=["не json", "без id", "пустой id", "не объект"],
)
async def test_unusable_success_body_is_retryable(response: httpx.Response) -> None:
    """Успешный, но неразобранный ответ — исход неизвестен, а не отказ.

    Платёж, скорее всего, принят; единственный безопасный ход — повтор
    с тем же ключом идемпотентности.
    """
    with pytest.raises(ProviderUnavailable):
        await _call(lambda _: response)


async def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(ProviderUnavailable):
        await _call(handler)


async def test_connection_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderUnavailable):
        await _call(handler)
