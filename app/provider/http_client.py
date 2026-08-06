"""HTTP-клиент внешнего провайдера.

Один вызов — одна попытка. Клиент намеренно не содержит цикла повторов:
расписание попыток живёт в таблице ``event_outbox`` и переживает
перезапуск процесса, а цикл внутри клиента не переживёт.

Классификация исходов здесь важнее самого запроса. Всё, что оставляет
судьбу платежа неизвестной — таймаут, обрыв, 5xx, 429, неразобранное тело
успешного ответа, — это ``ProviderUnavailable``: платёж мог быть создан,
поэтому единственный корректный ответ — повтор с тем же ключом. И только
осмысленный отказ 4xx означает, что запрос неверен сам по себе и повторять
его бесполезно.
"""

from __future__ import annotations

import json

import httpx
import structlog

from app.domain.errors import ProviderRejectedRequest, ProviderUnavailable
from app.provider.base import ProviderAcceptance

log = structlog.get_logger(__name__)

_PAYMENTS_PATH = "/payments"

# 429 стоит рядом с 5xx: провайдер жив, но просит подождать.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})


class HttpProviderClient:
    """Реализация ``ProviderClient`` поверх httpx."""

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=connect_timeout,
            ),
            # Повторы — забота диспетчера: httpx не знает про ключ
            # идемпотентности и про то, что попытку нужно записать в базу.
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    async def create_payment(
        self,
        *,
        idempotency_key: str,
        correlation_id: str,
        body: str,
    ) -> ProviderAcceptance:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": correlation_id,
        }

        try:
            # content, а не json=: тело сериализовано один раз при создании
            # намерения и уходит побайтово тем же, как требует контракт.
            response = await self._client.post(
                _PAYMENTS_PATH, content=body.encode("utf-8"), headers=headers
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"Таймаут вызова провайдера: {exc!r}") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Сетевой сбой при вызове провайдера: {exc!r}") from exc

        return self._interpret(response)

    def _interpret(self, response: httpx.Response) -> ProviderAcceptance:
        code = response.status_code

        if code >= 500 or code in _RETRYABLE_STATUSES:
            raise ProviderUnavailable(
                f"Провайдер ответил {code}: {_short_body(response)}"
            )

        if code >= 400:
            raise ProviderRejectedRequest(
                f"Провайдер отклонил запрос {code}: {_short_body(response)}",
                status_code=code,
            )

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            # Ответ успешный, но нечитаемый: платёж, скорее всего, принят.
            # Считаем исход неизвестным и повторяем с тем же ключом.
            raise ProviderUnavailable(
                f"Не удалось разобрать ответ провайдера {code}: {exc!r}"
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderUnavailable(f"Ответ провайдера не объект JSON: {payload!r}")

        provider_payment_id = payload.get("providerPaymentId")
        if not isinstance(provider_payment_id, str) or not provider_payment_id:
            raise ProviderUnavailable(
                f"В ответе провайдера нет providerPaymentId: {payload!r}"
            )

        status = payload.get("status")
        return ProviderAcceptance(
            provider_payment_id=provider_payment_id,
            status=status if isinstance(status, str) else "UNKNOWN",
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _short_body(response: httpx.Response) -> str:
    """Обрезанное тело для журнала: диагностика без простыней в логах."""
    try:
        text = response.text
    except Exception:  # pragma: no cover - тело уже недоступно
        return "<нечитаемо>"
    text = text.strip().replace("\n", " ")
    return text[:500] if text else "<пусто>"
