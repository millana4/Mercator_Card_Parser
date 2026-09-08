# -*- coding: utf-8 -*-
"""
handlers.py — обработчики задач select и parse.
Транспортный переходник: задача → вызов существующего OzonService → результат
с эхо-полями. Логику подбора/парсинга не меняет.
"""

from app.core.logging import get_logger
from app.core.exceptions import AntibotBlockedError, ProductNotFoundError, InvalidRequestError
from app.marketplaces.ozon.service import OzonService
from app.marketplaces.ozon.selection import StratumRequest, ExcludedCard

logger = get_logger(__name__)


def handle_select(task: dict, service: OzonService) -> dict:
    echo = {
        "task_id": task.get("task_id"),
        "set_id": task.get("set_id"),
        "stratum_id": task.get("stratum_id"),
        "geo": task.get("geo"),
        "marketplace": task.get("marketplace"),
    }
    logger.info("SELECT задача | task_id=%s set_id=%s stratum_id=%s "
                "категория=%s query=%r count=%s",
                echo["task_id"], echo["set_id"], echo["stratum_id"],
                task.get("category"), task.get("query"), task.get("count"))
    try:
        req = _build_stratum_request(task)
        result = service.select_for_stratum(req)
        cards = result["cards"]
        cards_data = [c.model_dump() if hasattr(c, "model_dump") else c for c in cards]
        return {
            **echo, "ok": True,
            "requested_count": result["requested"],
            "found_count": result["found"],
            "cards": cards_data, "error": None,
        }
    except Exception as e:
        logger.exception("SELECT задача провалилась | task_id=%s", echo["task_id"])
        return {
            **echo, "ok": False,
            "requested_count": task.get("count"), "found_count": 0,
            "cards": [], "error": _error_text(e), "error_code": _error_code(e),
        }


def handle_parse(task: dict, service: OzonService) -> dict:
    echo = {
        "task_id": task.get("task_id"),
        "card_id": task.get("card_id"),
        "sku": task.get("sku"),
        "geo": task.get("geo"),
        "marketplace": task.get("marketplace"),
    }
    logger.info("PARSE задача | task_id=%s card_id=%s sku=%s",
                echo["task_id"], echo["card_id"], echo["sku"])
    sku = task.get("sku")
    if not sku:
        logger.warning("PARSE задача без sku | task_id=%s", echo["task_id"])
        return {**echo, "ok": False, "card": None,
                "error": "В задаче отсутствует sku", "error_code": "invalid_request"}
    try:
        result = service.get_card_for_crawl_by_id(str(sku))
        return {**echo, "ok": True, "card": result["card"], "error": None}
    except Exception as e:
        logger.exception("PARSE задача провалилась | task_id=%s sku=%s", echo["task_id"], sku)
        return {**echo, "ok": False, "card": None,
                "error": _error_text(e), "error_code": _error_code(e)}


def _build_stratum_request(task: dict) -> StratumRequest:
    exclude = []
    for e in (task.get("exclude") or []):
        exclude.append(ExcludedCard(
            sku=str(e.get("sku")) if e.get("sku") is not None else None,
            name=e.get("name"),
            url=e.get("url"),
            seller=e.get("seller"),          # ← поле seller (см. правку в selection.py)
            collection=e.get("collection"),
        ))
    return StratumRequest(
        query=task["query"],
        count=task["count"],
        is_seasonal=task.get("is_seasonal", False),
        base_share=task.get("base_share"),
        exclude=exclude,
        category=task.get("category") or "clothing",
    )


def _error_text(e: Exception) -> str:
    if isinstance(e, AntibotBlockedError):
        return f"Антибот заблокировал: {e}"
    if isinstance(e, ProductNotFoundError):
        return f"Не найдено: {e}"
    if isinstance(e, InvalidRequestError):
        return f"Некорректный запрос: {e}"
    return f"{type(e).__name__}: {e}"

def _error_code(e: Exception) -> str:
    if isinstance(e, AntibotBlockedError):
        return "antibot_blocked"
    if isinstance(e, ProductNotFoundError):
        return "product_not_found"
    if isinstance(e, InvalidRequestError):
        return "invalid_request"
    return "internal_error"