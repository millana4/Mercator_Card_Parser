# -*- coding: utf-8 -*-
"""
logger.py — журнал событий парсинга.

Наблюдаемость обхода теперь обеспечивается: parser_id в
результатах, счётчики parser:<id>:done / :antibot в Redis, логи ядра. Если
понадобится структурированный поток событий — его можно поднять на Redis
(список/stream) здесь, не трогая вызовы emit_event в service.py.
"""
from app.core.logging import get_logger

logger = get_logger(__name__)


def emit_event(event_type: str, payload: dict) -> None:
    """No-op журнал: событие только логируется на debug, никуда не отправляется."""
    logger.debug("Событие (%s) не отправляется — журнал отключён: %s", event_type, payload)