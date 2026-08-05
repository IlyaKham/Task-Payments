"""Слой доступа к данным.

Репозитории — обычные функции, принимающие ``AsyncSession``. Ни одна из
них не коммитит: граница транзакции принадлежит вызывающему коду
(``session_scope`` или зависимость FastAPI). Благодаря этому правило
«одна квитанция — одна транзакция» контролируется в одном месте.
"""

from app.repositories import events, intents, operations, receipts

__all__ = ["events", "intents", "operations", "receipts"]
