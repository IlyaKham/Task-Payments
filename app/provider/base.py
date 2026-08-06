"""Порт для провайдера.

Диспетчер зависит от этого протокола, а не от httpx. Благодаря этому в
тестах подставляется поддельная реализация, которая умеет изображать
таймаут, 503 и потерю ответа после фактического принятия платежа — то
есть ровно те сценарии, которые проверяет автопроверка.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProviderAcceptance:
    """Ответ провайдера на создание платежа.

    Провайдер лишь принял платёж к обработке. Это не финальный статус:
    операцию завершает только callback-квитанция.
    """

    provider_payment_id: str
    status: str


@runtime_checkable
class ProviderClient(Protocol):
    async def create_payment(
        self,
        *,
        idempotency_key: str,
        correlation_id: str,
        body: str,
    ) -> ProviderAcceptance:
        """Выполняет одну попытку создания платежа.

        Повторы — забота вызывающего кода: их расписание хранится в базе и
        переживает перезапуск, тогда как цикл внутри клиента не переживёт.

        Бросает ``ProviderUnavailable`` при сбое, который стоит повторить,
        и ``ProviderRejectedRequest`` при неисправимой ошибке запроса.
        """
        ...

    async def aclose(self) -> None: ...
