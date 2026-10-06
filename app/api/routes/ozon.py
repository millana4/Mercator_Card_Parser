# -*- coding: utf-8 -*-
"""
ozon.py — маршруты Ozon.
  1. POST /ozon/card/by-url        — очищенная карточка по URL
  2. POST /ozon/card/by-id         — очищенная карточка по артикулу
  3. POST /ozon/select             — подбор карточек под страту
  4. POST /ozon/category/by-url    — информация о категории по URL
  5. POST /ozon/category/by-id     — информация о категории по ID
  6. POST /ozon/raw/by-url         — сырой __NUXT__ по URL
  7. POST /ozon/raw/by-id          — сырой __NUXT__ по артикулу
  8. POST /ozon/search/diagnostics — диагностика поисковой выдачи (служебный)
"""
import threading
from fastapi import APIRouter, Depends

from app.api.dependencies import verify_api_key
from app.core.logging import get_logger
from app.marketplaces.ozon.selection import StratumRequest
from app.marketplaces.ozon.service import OzonService
from app.marketplaces.ozon.schemas import (
    ParseByUrlRequest, ParseByIdRequest, CardResponse, RawResponse,
    CategoryInfoResponse, CategoryInfoByIdRequest, CategoryInfoByUrlRequest, SearchDiagRequest, SearchDiagResponse,
    SelectionResponse, SearchTilesRequest, SearchTilesResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/ozon", tags=["Ozon"])


_service: OzonService | None = None
_service_lock = threading.Lock()


def get_service() -> OzonService:
    """Один сервис на процесс API с долгоживущей сессией браузера (как у воркера):
    главная, переходы изнутри Ozon, браузер переживает запросы."""
    global _service
    with _service_lock:
        if _service is None:
            _service = OzonService(use_session=True)
        return _service


def shutdown_service() -> None:
    """Закрыть браузер при остановке API."""
    with _service_lock:
        if _service is not None:
            _service.close_crawl()


@router.post("/card/by-url", response_model=CardResponse,
             summary="Очищенная карточка по URL")
def card_by_url(req: ParseByUrlRequest,
                _: dict = Depends(verify_api_key),
                service: OzonService = Depends(get_service)):
    logger.info("POST /ozon/card/by-url url=%s", req.url)
    result = service.get_card(req.url)
    return CardResponse(card=result["card"], debug_files=result["debug_files"])


@router.post("/card/by-id", response_model=CardResponse,
             summary="Очищенная карточка по артикулу")
def card_by_id(req: ParseByIdRequest,
               _: dict = Depends(verify_api_key),
               service: OzonService = Depends(get_service)):
    logger.info("POST /ozon/card/by-id sku=%s", req.sku)
    result = service.get_card_by_id(req.sku)
    return CardResponse(card=result["card"], debug_files=result["debug_files"])


@router.post("/select", response_model=SelectionResponse,
             summary="Подбор карточек под страту сета категории")
def select(req: StratumRequest,
           _: dict = Depends(verify_api_key),
           service: OzonService = Depends(get_service)):
    logger.info(
        "POST /ozon/select ВХОД | query=%r count=%s seasonal=%s base_share=%s исключений=%s",
        req.query, req.count, req.is_seasonal, req.base_share, len(req.exclude),
    )
    r = service.select_for_stratum(req)
    picked_sku = [c.sku for c in r["cards"]]
    logger.info(
        "POST /ozon/select ВЫХОД | query=%r | запрошено=%s, подобрано=%s | sku=%s",
        req.query, r["requested"], r["found"], picked_sku,
    )
    return SelectionResponse(
        cards=r["cards"],
        requested_count=r["requested"],
        found_count=r["found"],
    )


@router.post("/category/by-url", response_model=CategoryInfoResponse,
             summary="Информация о категории по URL")
def category_info_by_url(req: CategoryInfoByUrlRequest,          # ← url
                  _: dict = Depends(verify_api_key),
                  service: OzonService = Depends(get_service)):
    logger.info("POST /ozon/category/by-url url=%s", req.url)
    info = service.get_category_info(req.url)
    return CategoryInfoResponse(
        category_id=info["category_id"],
        category_name=info["category_name"],
        offer_count=info["offer_count"],
    )


@router.post("/category/by-id", response_model=CategoryInfoResponse,
             summary="Информация о категории по ID")
def category_info_by_id(req: CategoryInfoByIdRequest,   # ← category_id
                        _: dict = Depends(verify_api_key),
                        service: OzonService = Depends(get_service)):
    logger.info("POST /ozon/category/by-id category_id=%s", req.category_id)
    info = service.get_category_info_by_id(req.category_id)
    return CategoryInfoResponse(
        category_id=info["category_id"],
        category_name=info["category_name"],
        offer_count=info["offer_count"],
    )


@router.post("/raw/by-url", response_model=RawResponse,
             summary="Сырой JSON страницы (__NUXT__) по URL")
def raw_by_url(req: ParseByUrlRequest,
               _: dict = Depends(verify_api_key),
               service: OzonService = Depends(get_service)):
    logger.info("POST /ozon/raw/by-url url=%s", req.url)
    result = service.get_raw(req.url)
    return RawResponse(sku=result["sku"], data=result["data"], debug_files=result["debug_files"])


@router.post("/raw/by-id", response_model=RawResponse,
             summary="Сырой JSON карточки (__NUXT__) по артикулу")
def raw_by_id(req: ParseByIdRequest,
              _: dict = Depends(verify_api_key),
              service: OzonService = Depends(get_service)):
    logger.info("POST /ozon/raw/by-id sku=%s", req.sku)
    result = service.get_raw_by_id(req.sku)
    return RawResponse(sku=result["sku"], data=result["data"], debug_files=result["debug_files"])


@router.post("/search/diagnostics", response_model=SearchDiagResponse,
             summary="Диагностика поисковой выдачи")
def search_diagnostics(req: SearchDiagRequest,
                       _: dict = Depends(verify_api_key),
                       service: OzonService = Depends(get_service)):
    logger.info("POST /ozon/search/diagnostics query=%s count=%s", req.query, req.count)
    r = service.search_diagnostics(req.query, req.count)
    return SearchDiagResponse(
        query=r["query"], url=r["url"], target_tiles=r["target_tiles"],
        extracted=r["extracted"], after_prefilter=r["after_prefilter"],
        candidates=r["candidates"], debug_files=r["debug_files"],
    )


@router.post("/search/tiles", response_model=SearchTilesResponse,
             summary="Снимок выдачи: плитки с позициями, ценой, скидкой, остатком")
def search_tiles(req: SearchTilesRequest,
                 _: dict = Depends(verify_api_key),
                 service: OzonService = Depends(get_service)):
    logger.info("POST /ozon/search/tiles query=%r target=%s", req.query, req.target)
    return SearchTilesResponse(**service.search_tiles(req.query, req.target))