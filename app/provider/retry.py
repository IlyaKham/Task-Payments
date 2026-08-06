"""Расчёт задержки перед следующей попыткой.

Экспоненциальный рост ограничен потолком, поверх накладывается случайный
разброс. Разброс нужен, чтобы пачка операций, отвалившихся одновременно
из-за одного сбоя провайдера, не пошла на повтор синхронно и не устроила
второй удар по только что поднявшемуся сервису.
"""

from __future__ import annotations

import random


def compute_backoff(
    attempt: int,
    *,
    base: float,
    maximum: float,
    jitter: float,
) -> float:
    """Задержка в секундах перед попыткой номер ``attempt`` (нумерация с 1)."""
    if attempt < 1:
        attempt = 1

    # Ограничиваем показатель степени, иначе при большом числе попыток
    # 2 ** attempt переполнит float ещё до сравнения с потолком.
    exponent = min(attempt - 1, 32)
    delay = min(maximum, base * float(2**exponent))

    if jitter > 0:
        spread = delay * jitter
        delay = delay + random.uniform(-spread, spread)

    return max(0.0, min(delay, maximum))
