# -*- coding: utf-8 -*-
"""
kafka_logger.py — журнал событий парсинга.

ИСТОРИЧЕСКАЯ ЗАМЕТКА: раньше события уходили в Kafka. После перехода на Redis
Kafka из сервиса убрана полностью. Журнал сохранён как точка расширения, но
сейчас это no-op: emit_event только пишет debug-лог и ничего никуда не шлёт.

Наблюдаемость обхода теперь обеспечивается иначе (см. воркер): parser_id в
результатах, счётчики parser:<id>:done / :antibot в Redis, логи ядра. Если
понадобится структурированный поток событий — его можно поднять на Redis
(список/stream) здесь, не трогая вызовы emit_event в service.py.
"""
from app.core.logging import get_logger

logger = get_logger(__name__)


def emit_event(event_type: str, payload: dict) -> None:
    """No-op журнал: событие только логируется на debug, никуда не отправляется."""
    logger.debug("Событие (%s) не отправляется — журнал отключён: %s", event_type, payload)