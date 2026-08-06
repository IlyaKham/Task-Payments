"""Проверки внешнего контракта, не требующие базы."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.api.schemas import CreateOperationRequest, format_amount, format_timestamp
from app.provider.retry import compute_backoff


def _request(**overrides: object) -> CreateOperationRequest:
    payload = {
        "operationId": "operation-123",
        "amount": "1000.00",
        "currency": "RUB",
        "description": "Оплата заказа",
    }
    payload.update(overrides)
    return CreateOperationRequest.model_validate(payload)


class TestAmount:
    def test_accepts_two_decimal_places(self) -> None:
        assert _request(amount="1000.00").amount == Decimal("1000.00")
        assert _request(amount="0.01").amount == Decimal("0.01")
        assert _request(amount="7").amount == Decimal("7.00")

    @pytest.mark.parametrize(
        "amount",
        ["0", "0.00", "-1.00", "1.234", "1e3", "abc", "", " ", "1_000"],
    )
    def test_rejects_invalid(self, amount: str) -> None:
        with pytest.raises(ValidationError):
            _request(amount=amount)

    def test_rejects_float(self) -> None:
        """float для денег теряет точность — на входе он недопустим."""
        with pytest.raises(ValidationError):
            _request(amount=1000.00)

    def test_output_always_has_two_places(self) -> None:
        assert format_amount(Decimal("7")) == "7.00"
        assert format_amount(Decimal("1000.5")) == "1000.50"


class TestCurrency:
    def test_normalises_case(self) -> None:
        assert _request(currency="rub").currency == "RUB"

    def test_rejects_unsupported(self) -> None:
        with pytest.raises(ValidationError):
            _request(currency="USD")


class TestTimestamp:
    def test_renders_utc_with_z(self) -> None:
        value = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
        assert format_timestamp(value) == "2026-07-15T12:00:00.000Z"

    def test_naive_treated_as_utc(self) -> None:
        assert format_timestamp(datetime(2026, 7, 15, 12, 0, 0)).endswith("Z")


class TestBackoff:
    def test_grows_and_stays_bounded(self) -> None:
        delays = [
            compute_backoff(attempt, base=0.5, maximum=15.0, jitter=0.0)
            for attempt in range(1, 12)
        ]
        assert delays[0] == 0.5
        assert delays == sorted(delays)
        assert max(delays) <= 15.0

    def test_jitter_stays_within_bounds(self) -> None:
        for _ in range(200):
            delay = compute_backoff(5, base=0.5, maximum=15.0, jitter=0.3)
            assert 0.0 <= delay <= 15.0

    def test_huge_attempt_does_not_overflow(self) -> None:
        assert compute_backoff(10_000, base=0.5, maximum=15.0, jitter=0.0) == 15.0

    def test_attempt_below_one_is_clamped(self) -> None:
        assert compute_backoff(0, base=0.5, maximum=15.0, jitter=0.0) == 0.5
