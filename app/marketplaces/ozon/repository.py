# -*- coding: utf-8 -*-
"""
repository.py — репозиторий-источник данных Ozon.

Паттерн Repository здесь абстрагирует ИСТОЧНИК сырья (сам сайт Ozon через
Selenium), а не базу данных. Слои выше не знают, как именно добываются данные —
они просто просят «дай сырьё по url/id». Маппинг в карточку тут НЕ делается
(это ответственность mapper.py), репозиторий отдаёт сырые html + nuxt_state.
"""

import re

from app.core.config import MarketplacePolicy
from app.core.logging import get_logger
from app.core.exceptions import InvalidRequestError
from app.marketplaces.ozon.parser import OzonParser
from app.marketplaces.ozon.nuxt import extract_nuxt_state
from app.marketplaces.ozon.search_listing import extract_next_page, extract_pagination

logger = get_logger(__name__)


class OzonRepository:
    """Достаёт сырьё карточки Ozon по URL или артикулу."""

    def __init__(self, policy: MarketplacePolicy):
        self.policy = policy

    @staticmethod
    def url_from_sku(sku: str) -> str:
        """Короткий URL карточки из артикула. Ozon редиректит на полный."""
        sku = sku.strip()
        if not sku.isdigit():
            raise InvalidRequestError(
                f"SKU должен быть числом, получено: {sku!r}", marketplace="ozon"
            )
        return f"https://www.ozon.ru/product/{sku}/"

    @staticmethod
    def sku_from_url(url: str) -> str | None:
        m = re.search(r"-(\d+)/?(?:\?|$)", url)
        return m.group(1) if m else None

    def fetch_raw(self, url: str) -> dict:
        """
        Открыть карточку и вернуть сырьё:
          {html, nuxt_state, screenshot_png}.
        Парсер создаётся и закрывается на каждый вызов (Selenium не разделяется).
        """
        url = url.strip()
        if not url:
            raise InvalidRequestError("Пустой URL", marketplace="ozon")

        logger.debug("OzonRepository.fetch_raw: url=%s", url)
        parser = OzonParser(self.policy)
        try:
            html = parser.fetch_html(url)
            not_found = ('data-widget="error"' in html
                         or "Такой страницы не существует" in html)
            nuxt = {} if not_found else extract_nuxt_state(html)
            png = parser.screenshot_png()
            return {
                "html": html,
                "nuxt_state": nuxt,
                "screenshot_png": png,
                "url": url,
                "not_found": not_found,
            }
        finally:
            parser.close()

    @staticmethod
    def search_url_from_query(query: str) -> str:
        """URL страницы поиска Ozon по текстовому запросу."""
        from urllib.parse import quote_plus
        q = (query or "").strip()
        if not q:
            raise InvalidRequestError("Пустой поисковый запрос", marketplace="ozon")
        return f"https://www.ozon.ru/search/?text={quote_plus(q)}"

    def fetch_search_raw(self, query: str | None = None, url: str | None = None) -> dict:
        """
        Открыть ОДНУ страницу выдачи. Вернуть:
          {html, url, next_page, current_page, total_pages, screenshot_png}.
        next_page — из HTML-паджинатора (может быть None).
        current_page/total_pages — из __NUXT__ (надёжны, есть всегда).
        """
        page_url = url or self.search_url_from_query(query)
        logger.debug("OzonRepository.fetch_search_raw: url=%s", page_url)
        parser = OzonParser(self.policy)
        try:
            html = parser.fetch_listing_html(page_url)
            nuxt = {}
            try:
                nuxt = extract_nuxt_state(html) or {}
            except Exception as e:
                logger.warning("Не удалось разобрать __NUXT__ выдачи: %s", e)
            current_page, total_pages = extract_pagination(nuxt)
            png = parser.screenshot_png()
            logger.info("Выдача получена: url=%s, currentPage=%s, totalPages=%s, nextPage=%s",
                        page_url, current_page, total_pages, bool(extract_next_page(html)))
            return {
                "html": html,
                "url": page_url,
                "next_page": extract_next_page(html),
                "current_page": current_page,
                "total_pages": total_pages,
                "screenshot_png": png,
            }
        finally:
            parser.close()