# -*- coding: utf-8 -*-
"""
service.py — бизнес-логика парсинга Ozon (оркестратор).

Дирижирует слоями, сам ничего не парсит и не извлекает:
  1) репозиторий достаёт сырьё (html + nuxt_state);
  2) mapper превращает сырьё в доменную карточку OzonCard;
  3) при DEBUG сохраняются артефакты (html/json/png) на диск;
  4) в Kafka уходит событие о результате (если включено).

Возвращает структуры, удобные для эндпоинтов (карточка / сырьё + debug-файлы).
"""

import os
import json
import time
from datetime import datetime

from app.core.config import settings, MarketplacePolicy
from app.core.exceptions import AntibotBlockedError, InvalidRequestError, ProductNotFoundError
from app.core.logging import get_logger
from app.marketplaces.ozon.repository import OzonRepository
from app.marketplaces.ozon.mapper import build_card
from app.marketplaces.ozon.selection import select_cards
from app.marketplaces.ozon.repository import OzonRepository
from app.marketplaces.ozon.parser import OzonParser
from app.shared.kafka_logger import emit_event

logger = get_logger(__name__)


class OzonService:
    def __init__(self, policy: MarketplacePolicy | None = None):
        self.policy = policy or settings.ozon_policy()
        self.repo = OzonRepository(self.policy)
        self._crawl_parser = None  # долгоживущий парсер для сессии

    # ------------------------------------------------------------------ #
    def _basename(self, url: str) -> str:
        sku = OzonRepository.sku_from_url(url) or datetime.now().strftime("%Y%m%d_%H%M%S")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"ozon_{sku}_{ts}"

    def _save_debug(self, base: str, raw: dict, card_dict: dict | None) -> dict:
        """Сохранить артефакты в debug_dir. Только при settings.debug."""
        if not settings.debug:
            return {}
        os.makedirs(settings.debug_dir, exist_ok=True)
        files: dict[str, str] = {}

        html_path = os.path.join(settings.debug_dir, f"{base}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(raw["html"])
        files["html"] = os.path.basename(html_path)

        json_path = os.path.join(settings.debug_dir, f"{base}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(raw["nuxt_state"], f, ensure_ascii=False, indent=2)
        files["json"] = os.path.basename(json_path)

        if card_dict is not None:
            card_path = os.path.join(settings.debug_dir, f"{base}_card.json")
            with open(card_path, "w", encoding="utf-8") as f:
                json.dump(card_dict, f, ensure_ascii=False, indent=2)
            files["card"] = os.path.basename(card_path)

        if raw.get("screenshot_png"):
            png_path = os.path.join(settings.debug_dir, f"{base}.png")
            with open(png_path, "wb") as f:
                f.write(raw["screenshot_png"])
            files["png"] = os.path.basename(png_path)

        logger.debug("Сохранены debug-файлы: %s", files)
        return files

    # ------------------------------------------------------------------ #
    def get_card(self, url: str) -> dict:
        """Полный разбор: вернуть структурированную карточку + debug-файлы."""
        t0 = time.monotonic()
        logger.info("get_card: старт, url=%s", url)
        try:
            raw = self.repo.fetch_raw(url)
        except AntibotBlockedError as exc:
            self._emit_blocked(url, exc)
            raise
        card = build_card(raw["html"], raw["nuxt_state"], raw["url"])
        card_dict = card.model_dump()
        files = self._save_debug(self._basename(raw["url"]), raw, card_dict)
        took = round(time.monotonic() - t0, 2)
        emit_event("ozon.card.parsed",
                   {"url": raw["url"], "sku": card.sku, "took_s": took})
        logger.info("get_card: готово за %sс, sku=%s", took, card.sku)
        return {"card": card_dict, "debug_files": files or None}

    def get_raw(self, url: str) -> dict:
        """Только сырьё: вернуть __NUXT__ JSON + debug-файлы."""
        t0 = time.monotonic()
        logger.info("get_raw: старт, url=%s", url)
        try:
            raw = self.repo.fetch_raw(url)
        except AntibotBlockedError as exc:
            self._emit_blocked(url, exc)
            raise
        sku = OzonRepository.sku_from_url(raw["url"])
        files = self._save_debug(self._basename(raw["url"]), raw, None)
        took = round(time.monotonic() - t0, 2)
        emit_event("ozon.raw.parsed",
                   {"url": raw["url"], "sku": sku, "took_s": took})
        logger.info("get_raw: готово за %sс, sku=%s", took, sku)
        return {"sku": sku, "data": raw["nuxt_state"], "debug_files": files or None}

    # by-id обёртки
    def get_card_by_id(self, sku: str) -> dict:
        return self.get_card(OzonRepository.url_from_sku(sku))

    def get_raw_by_id(self, sku: str) -> dict:
        return self.get_raw(OzonRepository.url_from_sku(sku))

    # ================= Обход с долгоживущей сессией (воркер) ================= #
    def _ensure_crawl_session(self) -> None:
        """Гарантировать живой парсер с активной сессией. Создаёт браузер и
        начинает сессию, если его нет или сессия исчерпана."""
        if self._crawl_parser is not None and not self._crawl_parser.session_exhausted():
            return
        # Старую сессию (исчерпанную) закрываем.
        self._close_crawl_session()
        logger.info("Создаю новую сессию обхода")
        self._crawl_parser = OzonParser(self.policy)
        self._crawl_parser.begin_session()

    def _close_crawl_session(self) -> None:
        """Закрыть текущий парсер обхода (при исчерпании сессии или бане)."""
        if self._crawl_parser is not None:
            try:
                self._crawl_parser.close()
            except Exception as e:
                logger.debug("Ошибка закрытия сессии обхода: %s", e)
            self._crawl_parser = None

    def get_card_for_crawl(self, url: str) -> dict:
        """Разобрать карточку в рамках долгоживущей сессии (путь ОБХОДА).
        Управляет сессией: заводит браузер, считает переходы, пересоздаёт по
        исчерпании. При антиботе закрывает сессию и пробрасывает исключение —
        воркер уйдёт в cooldown. HTTP-путь (get_card) не затрагивается."""
        t0 = time.monotonic()
        logger.info("get_card_for_crawl: старт, url=%s", url)
        self._ensure_crawl_session()
        parser = self._crawl_parser
        try:
            html = parser.fetch_html_in_session(url)
        except AntibotBlockedError as exc:
            self._emit_blocked(url, exc)
            self._close_crawl_session()  # бан прерывает всю сессию
            raise
        finally:
            # Переход израсходован в любом случае (даже при 404).
            if parser is not None:
                parser.consume_session_step()

        not_found = ('data-widget="error"' in html
                     or "Такой страницы не существует" in html)
        nuxt = {} if not_found else parser.extract_nuxt_state(html)
        card = build_card(html, nuxt, url)
        card_dict = card.model_dump()
        took = round(time.monotonic() - t0, 2)
        emit_event("ozon.card.parsed",
                   {"url": url, "sku": card.sku, "took_s": took, "crawl": True})
        logger.info("get_card_for_crawl: готово за %sс, sku=%s, сессия_осталось=%s",
                    took, card.sku, parser._session_left if parser else "-")
        return {"card": card_dict, "debug_files": None}

    def get_card_for_crawl_by_id(self, sku: str) -> dict:
        return self.get_card_for_crawl(OzonRepository.url_from_sku(sku))

    def close_crawl(self) -> None:
        """Публичное закрытие сессии обхода (для остановки воркера)."""
        self._close_crawl_session()

    # ------------------------------------------------------------------ #
    def _emit_blocked(self, url: str, exc: AntibotBlockedError) -> None:
        """
        Зафиксировать факт блокировки антиботом.

        Сейчас событие уходит в Kafka через emit_event (при KAFKA_ENABLED=false
        просто пишется в лог уровня WARNING и дальше не идёт — мягкая деградация).
        Когда брокер подключат, эти же события начнут собираться для анализа
        неслучайности пропусков (какие товары/когда чаще блокируются).
        """
        sku = OzonRepository.sku_from_url(url)
        logger.warning("Антибот заблокировал парсинг: url=%s sku=%s", url, sku)
        emit_event("ozon.blocked",
                   {"url": url, "sku": sku, "reason": str(exc)})

    # ========================================================================== #
    #  Category info                                                             #
    # ========================================================================== #

    def get_category_info(self, url: str) -> dict:
        """Получить ID, название и количество товаров в категории."""
        t0 = time.monotonic()
        logger.info("get_category_info: старт, url=%s", url)

        url = (url or "").strip()
        if not url or "/category/" not in url:
            raise InvalidRequestError(
                f"Некорректный URL категории Ozon: {url!r}", marketplace="ozon"
            )

        try:
            raw = self.repo.fetch_raw(url)
        except AntibotBlockedError as exc:
            self._emit_blocked(url, exc)
            raise

        data = raw["nuxt_state"]
        result = self._extract_category_info(data)

        if result["category_id"] is None and result["category_name"] is None:
            raise ProductNotFoundError(
                "Данные категории не найдены на странице.", marketplace="ozon"
            )

        took = round(time.monotonic() - t0, 2)
        logger.info("get_category_info: готово за %sс, category_id=%s, offer_count=%s",
                    took, result.get("category_id"), result.get("offer_count"))

        return result

    def _extract_category_info(self, data: dict) -> dict:
        """Извлекает ID, название и offerCount из __NUXT__.state."""
        result = {
            "category_id": None,
            "category_name": None,
            "offer_count": None,
        }

        # 1. Данные категории из shared.catalog
        shared = data.get("shared", {})
        catalog = shared.get("catalog", {})
        category = catalog.get("category", {})

        if category.get("id") is not None:
            try:
                result["category_id"] = int(category["id"])
            except (TypeError, ValueError):
                logger.warning("category_id не является числом: %r", category.get("id"))
                result["category_id"] = None
        if category.get("name"):
            result["category_name"] = category["name"]

        # 2. offerCount из SEO-микроразметки
        seo = data.get("seo", {})
        scripts = seo.get("script", [])

        for script in scripts:
            if script.get("type") == "application/ld+json":
                try:
                    inner = json.loads(script.get("innerHTML", "{}"))
                    if "offers" in inner and "offerCount" in inner["offers"]:
                        result["offer_count"] = inner["offers"]["offerCount"]
                        break
                except Exception:
                    continue

        return result

    @staticmethod
    def category_url_from_id(category_id: str) -> str:
        """URL категории из ID. Ozon редиректит на канонический адрес со slug."""
        category_id = str(category_id).strip()
        if not category_id.isdigit():
            raise InvalidRequestError(
                f"ID категории должен быть числом, получено: {category_id!r}",
                marketplace="ozon",
            )
        return f"https://www.ozon.ru/category/{category_id}/"

    def get_category_info_by_id(self, category_id: str) -> dict:
        """Информация о категории по её ID (через построение URL)."""
        return self.get_category_info(self.category_url_from_id(category_id))

    def search_diagnostics(self, query: str, count: int) -> dict:
        """Дагностика сбора сета: открыть первую страницу выдачи, извлечь кандидатов, отфильтровать."""
        from app.marketplaces.ozon.search_listing import extract_candidates, prefilter
        try:
            raw = self.repo.fetch_search_raw(query=query)
        except AntibotBlockedError as exc:
            self._emit_blocked(query, exc)
            raise
        cands = extract_candidates(raw["html"])
        pf = prefilter(cands, [], self.policy.min_rating, self.policy.min_reviews)
        files = {}
        if settings.debug:
            import os
            os.makedirs(settings.debug_dir, exist_ok=True)
            p = os.path.join(settings.debug_dir,
                             f"search_{datetime.now():%Y%m%d_%H%M%S}.html")
            with open(p, "w", encoding="utf-8") as f:
                f.write(raw["html"])
            files["html"] = os.path.basename(p)
        return {"query": query, "url": raw["url"], "target_tiles": count,
                "extracted": len(cands), "after_prefilter": len(pf),
                "next_page": raw["next_page"],
                "candidates": [c.model_dump() for c in pf], "debug_files": files or None}

    def select_for_stratum(self, req) -> dict:
        """
        Подбор карточек под страту. Оркестрация в selection.select_cards,
        карточки берутся существующим get_card (тот же путь, что card/by-url).
        """

        logger.info("Подбор для страты: query=%r count=%s seasonal=%s base_share=%s исключений=%s",
                    req.query, req.count, req.is_seasonal, req.base_share, len(req.exclude))

        def fetch_page(url):
            # первая страница — по query (url=None), следующие — по nextPage
            return self.repo.fetch_search_raw(query=req.query if url is None else None, url=url)

        def get_card_only(card_url):
            # get_card возвращает dict {"card":..., "debug_files":...}; берём OzonCard
            res = self.get_card(card_url)
            # res["card"] — это dict; превратим обратно в OzonCard для единообразия ответа
            from app.marketplaces.ozon.models import OzonCard
            return OzonCard(**res["card"])

        result = select_cards(
            req,
            fetch_page=fetch_page,
            get_card=get_card_only,
            min_rating=self.policy.min_rating,
            min_reviews=self.policy.min_reviews,
            max_pages=self.policy.max_pages,
        )
        return result