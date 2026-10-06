# -*- coding: utf-8 -*-
"""
wb.py — маршруты Wildberries.
  1. POST /wb/raw/by-url — сырая страница WB по URL (разведка: HTML + скриншот)
"""
from fastapi import APIRouter, Depends

from app.api.dependencies import verify_api_key
from app.core.logging import get_logger
from app.marketplaces.wb.schemas import WbRawByUrlRequest, WbRawResponse
from app.marketplaces.wb.service import WbService

logger = get_logger(__name__)

router = APIRouter(prefix="/wb", tags=["Wildberries"])


def get_service() -> WbService:
    """Фабрика сервиса (можно подменить в тестах)."""
    return WbService()


@router.post("/raw/by-url", response_model=WbRawResponse,
             summary="Сырая страница WB по URL (разведка: HTML и скриншот)")
def raw_by_url(req: WbRawByUrlRequest,
               _: dict = Depends(verify_api_key),
               service: WbService = Depends(get_service)):
    logger.info("POST /wb/raw/by-url url=%s extra_wait_s=%s", req.url, req.extra_wait_s)
    return WbRawResponse(**service.get_raw_page(req.url, req.extra_wait_s))