# -*- coding: utf-8 -*-
"""
service.py — бизнес-логика парсинга Ozon (оркестратор).

Дирижирует слоями, сам ничего не парсит и не извлекает:
  1) источник отдаёт сырьё (html + nuxt_state);
  2) mapper превращает сырьё в доменную карточку OzonCard;
  3) при DEBUG сохраняются артефакты (html/json/png) на диск;
  4) в Kafka уходит событие о результате (если включено).

Два режима получения страниц:
  - use_session=True (воркер и HTTP API): две долгоживущие сессии браузера,
    у каждой свой профиль и свой экран Xvfb:
      * сессия обхода  — главная и карточки обхода (get_card_for_crawl);
      * сессия выдачи  — главная, страницы поиска (снимки выдачи, диагностика)
        и карточки HTTP get_card.
    Сессия обхода никогда не открывает страницу поиска: вердикт антибота,
    полученный на выдаче, остаётся в сессии выдачи.
    Вызовы выполняются по очереди под общей блокировкой;
  - подбор (select_for_stratum) — два своих браузера на задачу, отдельно от сессий
    обхода и выдачи: браузер выдачи (поиск открыт всю задачу, догрузка прокруткой,
    кандидаты — с отрисованных плиток) и браузер карточек (только карточки-кандидаты).
    Выдача и карточки в одном браузере не смешиваются
    (до 13.10 переход «выдача → карточка» в одном браузере стабильно давал капчу).
    Каждый браузер начинает с главной Ozon и переходит изнутри Ozon (с Referer),
    лимит переходов — как у сессии обхода; капча — браузер закрывается, его профиль
    очищается; в конце задачи оба закрываются, профили с кукой остаются;
  - use_session=False (только диагностика): новый браузер на каждый вызов
    через repository.

Постоянные профили:
OZON_CHROME_PROFILE_DIR — сессия обхода,
OZON_SEARCH_PROFILE_DIR — сессия выдачи,
OZON_SELECT_LISTING_PROFILE_DIR и OZON_SELECT_CARDS_PROFILE_DIR — браузеры выдачи и карточек подбора.
Не задан — временный профиль.
"""

import os
import json
import random
import shutil
import threading
import time
from datetime import datetime, timezone

from app.core.config import settings, MarketplacePolicy
from app.core.exceptions import (AntibotBlockedError, InvalidRequestError, OutOfStockError,
                                 PageLoadError, ProductNotFoundError)
from app.core.logging import get_logger
from selenium.common.exceptions import WebDriverException
from app.marketplaces.ozon.mapper import build_card
from app.marketplaces.ozon.models import OzonCard
from app.marketplaces.ozon.device_profile import DeviceProfile, pick_device
from app.marketplaces.ozon.nuxt import extract_nuxt_state
from app.marketplaces.ozon.parser import OzonParser
from app.marketplaces.ozon.repository import OzonRepository
from app.marketplaces.ozon.search_listing import (candidates_from_tiles, extract_next_page,
                                                  extract_pagination, parse_tiles)
from app.marketplaces.ozon.selection import select_cards
from app.shared.logger import emit_event

logger = get_logger(__name__)


# Префикс снимков блокировок в debug_dir (сохраняются и без DEBUG, с ротацией).
BLOCK_PREFIX = "block_"

# Сессии браузера: crawl — обход карточек, search — выдача и подбор.
CRAWL = "crawl"
SEARCH = "search"
SESSIONS = (CRAWL, SEARCH)
_SESSION_ATTR = {CRAWL: "_crawl_parser", SEARCH: "_search_parser"}
_SESSION_PROFILE_ENV = {CRAWL: "OZON_CHROME_PROFILE_DIR", SEARCH: "OZON_SEARCH_PROFILE_DIR"}
_SESSION_TITLE = {CRAWL: "обхода", SEARCH: "выдачи"}

# Браузеры подбора: свои профили, отдельно от сессий обхода и выдачи.
SELECT_LISTING = "select_listing"
SELECT_CARDS = "select_cards"
_SELECT_PROFILE_ENV = {SELECT_LISTING: "OZON_SELECT_LISTING_PROFILE_DIR",
                       SELECT_CARDS: "OZON_SELECT_CARDS_PROFILE_DIR"}
_SELECT_TITLE = {SELECT_LISTING: "выдачи подбора", SELECT_CARDS: "карточек подбора"}


def _wipe_profile(profile_dir: str | None) -> bool:
    """Удалить содержимое постоянного профиля Chrome (сам каталог - том - остаётся).
    False - профиль не задан или каталога нет."""
    if not profile_dir or not os.path.isdir(profile_dir):
        return False
    for entry in os.listdir(profile_dir):
        path = os.path.join(profile_dir, entry)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception as e:
            logger.warning("Не удалось удалить %s из профиля: %s", path, e)
    return True

class NavigationPacer:
    """Пауза между переходами внутри одной задачи (подбор: страницы выдачи и
    карточки-кандидаты). Перед первым переходом не ждём — паузу после прошлой
    задачи уже выдержал воркер; перед каждым следующим — случайная пауза
    из [lo, hi]."""

    def __init__(self, lo: float, hi: float, sleep=None):
        self.lo = lo
        self.hi = hi
        self._sleep = sleep or time.sleep
        self._first = True

    def wait(self) -> None:
        if self._first:
            self._first = False
            return
        delay = random.uniform(self.lo, self.hi)
        logger.info("Пауза перед переходом подбора: %.0f с", delay)
        self._sleep(delay)


class OzonService:
    def __init__(self, policy: MarketplacePolicy | None = None, use_session: bool = False):
        self.policy = policy or settings.ozon_policy()
        self.repo = OzonRepository(self.policy)
        self.use_session = use_session
        self._crawl_parser = None       # долгоживущий парсер сессии обхода
        self._search_parser = None      # долгоживущий парсер сессии выдачи
        self._lock = threading.RLock()  # браузеры - вызовы по очереди
        self._sleep = time.sleep        # пауза между переходами подбора
        # Поколение устройства браузера по сессиям (растёт при бане своей сессии)
        self._device_generation = {CRAWL: 0, SEARCH: 0}

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

    # ================= Источник страниц: новый браузер или сессия ================= #
    def _fetch_raw(self, url: str, full_content: bool = False, fresh: bool = False) -> dict:
        """Сырьё карточки/страницы {html, nuxt_state, screenshot_png, url, not_found}.
        fresh или режим без сессии — новый браузер с временным профилем (repository).
        Иначе — долгоживущий браузер сессии выдачи (HTTP); при антиботе сессия закрывается.
        OutOfStockError и AntibotBlockedError пробрасываются."""
        if fresh or not self.use_session:
            return self.repo.fetch_raw(url, full_content=full_content)
        url = (url or "").strip()
        if not url:
            raise InvalidRequestError("Пустой URL", marketplace="ozon")
        parser = self._ensure_session(SEARCH)
        try:
            html = parser.fetch_html_in_session(url, full_content=full_content)
        except AntibotBlockedError as exc:
            self._close_after_block(SEARCH, parser, exc, url)
            raise
        except WebDriverException as e:
            raise self._browser_failure(SEARCH, parser, e, url) from e
        finally:
            parser.consume_session_step()
        not_found = ('data-widget="error"' in html
                     or "Такой страницы не существует" in html)
        nuxt = {} if not_found else extract_nuxt_state(html)
        return {"html": html, "nuxt_state": nuxt, "screenshot_png": parser.screenshot_png(),
                "url": url, "not_found": not_found}

    def _listing_in_session(self, page_url: str, scroll_steps: int) -> dict:
        """Одна страница выдачи в сессии выдачи (с прокруткой или без):
        {html, url, next_page, current_page, total_pages, screenshot_png}."""
        parser = self._ensure_session(SEARCH)
        try:
            html = parser.fetch_listing_scrolled(page_url, scroll_steps)
        except AntibotBlockedError as exc:
            self._close_after_block(SEARCH, parser, exc, page_url)
            raise
        except WebDriverException as e:
            raise self._browser_failure(SEARCH, parser, e, page_url) from e
        finally:
            parser.consume_session_step()
        return self._listing_raw(html, page_url, parser)

    @staticmethod
    def _listing_raw(html: str, page_url: str, parser) -> dict:
        """Сырьё страницы выдачи из HTML: пагинация из __NUXT__, nextPage из паджинатора."""
        nuxt = {}
        try:
            nuxt = extract_nuxt_state(html) or {}
        except Exception as e:
            logger.warning("Не удалось разобрать __NUXT__ выдачи: %s", e)
        current_page, total_pages = extract_pagination(nuxt)
        return {"html": html, "url": page_url, "next_page": extract_next_page(html),
                "current_page": current_page, "total_pages": total_pages,
                "screenshot_png": parser.screenshot_png()}

    def _fetch_listing(self, query: str | None = None, url: str | None = None,
                       fresh: bool = False) -> dict:
        """Страница выдачи для подбора и диагностики (без прокрутки).
        fresh или режим без сессии — новый браузер с временным профилем (repository)."""
        if fresh or not self.use_session:
            return self.repo.fetch_search_raw(query=query, url=url)
        page_url = url or OzonRepository.search_url_from_query(query)
        return self._listing_in_session(page_url, scroll_steps=0)

    # ------------------------------------------------------------------ #
    def get_card(self, url: str, full_content: bool = True, fresh: bool = False) -> dict:
        """Полный разбор: вернуть структурированную карточку + debug-файлы.
        full_content — дождаться полного блока характеристик (подбор и HTTP).
        fresh — новый браузер с временным профилем (подбор)."""
        t0 = time.monotonic()
        logger.info("get_card: старт, url=%s (сессия=%s, новый браузер=%s)",
                    url, self.use_session, fresh)
        with self._lock:
            try:
                raw = self._fetch_raw(url, full_content=full_content, fresh=fresh)
            except OutOfStockError:
                if fresh or not self.use_session:
                    raise
                # Товар закончился (Ozon увёл на поиск) - не бан, карточка с нашим sku
                card = OzonCard(
                    sku=OzonParser._sku_from_url(url), url=url,
                    parsed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    button_state="out_of_stock",
                )
                logger.info("get_card: товар закончился, sku=%s", card.sku)
                return {"card": card.model_dump(), "debug_files": None}
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
        logger.info("get_raw: старт, url=%s (сессия=%s)", url, self.use_session)
        with self._lock:
            try:
                raw = self._fetch_raw(url)
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

    # ================= Долгоживущие сессии браузера (воркер и HTTP) ================= #
    def set_device_generation(self, generation: int, session: str | None = None) -> None:
        """Поколение устройства браузера: берётся из состояния бана в Redis.
        session — для одной сессии (бан этой сессии), None — для всех (старт воркера).
        Новое устройство применяется со следующего браузера этой сессии."""
        for name in ([session] if session else SESSIONS):
            self._device_generation[name] = max(int(generation), 0)

    def _device(self, session: str = CRAWL) -> DeviceProfile:
        """Устройство браузера для текущего парсера и поколения сессии."""
        return pick_device(os.environ.get("PARSER_ID", "0"), self._device_generation[session])

    def _session_parser(self, session: str):
        return getattr(self, _SESSION_ATTR[session])

    def _ensure_session(self, session: str):
        """Живой парсер сессии с неисчерпанным лимитом переходов. Создаёт браузер
        (своё устройство, свой профиль) и начинает сессию, если его нет или
        лимит переходов исчерпан. Возвращает парсер."""
        parser = self._session_parser(session)
        if parser is not None and not parser.session_exhausted():
            return parser
        self._close_session(session)
        logger.info("Создаю новую сессию %s", _SESSION_TITLE[session])
        # Пустая строка — временный профиль (переменная не задана).
        profile_dir = os.environ.get(_SESSION_PROFILE_ENV[session]) or ""
        parser = OzonParser(self.policy, device=self._device(session), profile_dir=profile_dir)
        setattr(self, _SESSION_ATTR[session], parser)
        parser.begin_session()
        return parser

    def _close_session(self, session: str) -> None:
        """Закрыть браузер сессии (при исчерпании лимита, бане или остановке)."""
        parser = self._session_parser(session)
        if parser is not None:
            try:
                parser.close()
            except Exception as e:
                logger.debug("Ошибка закрытия сессии %s: %s", _SESSION_TITLE[session], e)
            setattr(self, _SESSION_ATTR[session], None)

    def _close_after_block(self, session: str, parser, exc: AntibotBlockedError, url: str) -> None:
        """Блокировка или сбой загрузки: сохранить title, адрес и скриншот,
        закрыть браузер сессии — следующая задача этой сессии начнёт новую."""
        self._save_block_artifacts(parser, exc, url, session=session)
        self._close_session(session)

    def _browser_failure(self, session: str, parser, e: WebDriverException, url: str) -> PageLoadError:
        """Ошибка браузера (таймаут рендерера, браузер не отвечает) — сбой загрузки:
        снимок, закрытие сессии. Воркер считает сбои подряд и при серии уходит в паузу."""
        first_line = (getattr(e, "msg", None) or type(e).__name__).splitlines()[0][:200]
        failure = PageLoadError(f"Браузер не отвечает: {first_line}", marketplace="ozon")
        self._close_after_block(session, parser, failure, url)
        return failure

    def _save_block_artifacts(self, parser, exc: AntibotBlockedError, url: str,
                              session: str | None = None) -> None:
        """Снимок блокировки в debug_dir: json (вид, сессия, причина, title, адреса) + png.
        Сохраняется и без DEBUG; хранятся последние policy.block_artifacts_keep."""
        try:
            snap = parser.block_snapshot()
            os.makedirs(settings.debug_dir, exist_ok=True)
            base = f"{BLOCK_PREFIX}{datetime.now():%Y%m%d_%H%M%S_%f}"
            info = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": "load" if isinstance(exc, PageLoadError) else "ban",
                "session": session,
                "reason": str(exc),
                "requested_url": url,
                "title": snap.get("title"),
                "current_url": snap.get("url"),
            }
            with open(os.path.join(settings.debug_dir, f"{base}.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
            if snap.get("png"):
                with open(os.path.join(settings.debug_dir, f"{base}.png"), "wb") as f:
                    f.write(snap["png"])
            logger.warning("Снимок блокировки сохранён: %s (вид=%s, сессия=%s, title=%r, url=%r)",
                           base, info["kind"], session, info["title"], info["current_url"])
            self._rotate_block_artifacts()
        except Exception as e:
            logger.warning("Не удалось сохранить снимок блокировки: %s", e)

    def _rotate_block_artifacts(self) -> None:
        """Оставить в debug_dir только последние block_artifacts_keep снимков."""
        keep = self.policy.block_artifacts_keep
        names = [n for n in os.listdir(settings.debug_dir) if n.startswith(BLOCK_PREFIX)]
        bases = sorted({n.rsplit(".", 1)[0] for n in names})
        old = bases[:-keep] if keep > 0 else bases
        for base in old:
            for ext in (".json", ".png"):
                path = os.path.join(settings.debug_dir, base + ext)
                if os.path.exists(path):
                    os.remove(path)

    def reset_profile(self, session: str | None = None) -> None:
        """Бан: закрыть браузер сессии и удалить содержимое её постоянного
        профиля — следующая сессия начнётся с чистой личностью гостя.
        session=None — все сессии."""
        with self._lock:
            for name in ([session] if session else SESSIONS):
                self._close_session(name)
                profile_dir = os.environ.get(_SESSION_PROFILE_ENV[name])
                if _wipe_profile(profile_dir):
                    logger.warning("Профиль браузера сессии %s сброшен: %s",
                                   _SESSION_TITLE[name], profile_dir)

    def get_card_for_crawl(self, url: str, full_content: bool = False) -> dict:
        """Разобрать карточку в рамках долгоживущей сессии (путь ОБХОДА).
        Управляет сессией: заводит браузер, считает переходы, пересоздаёт по
        исчерпании. При антиботе закрывает сессию и пробрасывает исключение —
        воркер уйдёт в cooldown. HTTP-путь (get_card) не затрагивается."""
        t0 = time.monotonic()
        logger.info("get_card_for_crawl: старт, url=%s", url)
        parser = self._ensure_session(CRAWL)
        try:
            html = parser.fetch_html_in_session(url, full_content=full_content)
        except OutOfStockError:
            # Товар закончился — НЕ бан, сессию НЕ рвём. Карточку строим вручную
            # с нашим sku: build_card на HTML страницы поиска дал бы чужой sku
            # (из выдачи аналогов). Цены пустые — товара нет.
            sku = parser._sku_from_url(url)
            card = OzonCard(
                sku=sku,
                url=url,
                parsed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                button_state="out_of_stock",
            )
            logger.info("get_card_for_crawl: товар закончился, sku=%s", sku)
            return {"card": card.model_dump(), "debug_files": None}
        except AntibotBlockedError as exc:
            self._emit_blocked(url, exc)
            self._close_after_block(CRAWL, parser, exc, url)  # бан или сбой загрузки прерывает сессию
            raise
        except WebDriverException as e:
            failure = self._browser_failure(CRAWL, parser, e, url)
            self._emit_blocked(url, failure)
            raise failure from e
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

    def get_card_for_crawl_by_id(self, sku: str, full_content: bool = False) -> dict:
        return self.get_card_for_crawl(OzonRepository.url_from_sku(sku), full_content=full_content)

    def close_crawl(self) -> None:
        """Публичное закрытие всех сессий браузера (остановка воркера или API)."""
        with self._lock:
            for name in SESSIONS:
                self._close_session(name)

    # ------------------------------------------------------------------ #
    def _emit_blocked(self, url: str, exc: AntibotBlockedError) -> None:
        """
        Зафиксировать факт блокировки антиботом.

        Сейчас событие уходит в Kafka через emit_event (при KAFKA_ENABLED=false
        просто пишется в лог уровня WARNING и дальше не идёт — мягкая деградация).
        """
        sku = OzonRepository.sku_from_url(url)
        what = "Сбой загрузки" if isinstance(exc, PageLoadError) else "Антибот заблокировал парсинг"
        logger.warning("%s: url=%s sku=%s", what, url, sku)
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

        with self._lock:
            try:
                raw = self._fetch_raw(url)
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
        """Диагностика сбора сета: открыть первую страницу выдачи, извлечь кандидатов, отфильтровать."""
        from app.marketplaces.ozon.search_listing import extract_candidates, prefilter
        with self._lock:
            try:
                raw = self._fetch_listing(query=query)
            except AntibotBlockedError as exc:
                self._emit_blocked(query, exc)
                raise
        cands = extract_candidates(raw["html"])
        pf = prefilter(cands, [], self.policy.min_rating, self.policy.min_reviews)
        files = {}
        if settings.debug:
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

    def _tiles_in_session(self, page_url: str, target: int) -> dict:
        """Выдача в сессии выдачи: переход, прокрутка до target плиток,
        сбор отрисованных плиток. {raw_tiles, scrolls, html, url, screenshot_png}."""
        parser = self._ensure_session(SEARCH)
        try:
            parser.fetch_listing_scrolled(page_url, 0)
            raw_tiles, scrolls = parser.scroll_collect_tiles(target, self.policy.search_max_scrolls)
            html = parser.driver.page_source
            png = parser.screenshot_png()
        except AntibotBlockedError as exc:
            self._close_after_block(SEARCH, parser, exc, page_url)
            raise
        except WebDriverException as e:
            raise self._browser_failure(SEARCH, parser, e, page_url) from e
        finally:
            parser.consume_session_step()
        return {"raw_tiles": raw_tiles, "scrolls": scrolls, "html": html,
                "url": page_url, "screenshot_png": png}

    def search_tiles(self, query: str, target: int | None = None, save_files: bool = True) -> dict:
        """Снимок выдачи: плитки с позициями, в карточки не заходим.
        save_files - сохранить HTML и скриншот (Swagger - всегда, воркер - при DEBUG)."""
        from app.marketplaces.ozon.search_listing import parse_tiles
        target = target or self.policy.search_tiles_target
        page_url = OzonRepository.search_url_from_query(query)
        with self._lock:
            try:
                raw = self._tiles_in_session(page_url, target)
            except AntibotBlockedError as exc:
                self._emit_blocked(query, exc)
                raise

        tiles = [t.model_dump() for t in parse_tiles(raw["raw_tiles"], target)]

        files: dict[str, str] = {}
        if save_files:
            os.makedirs(settings.debug_dir, exist_ok=True)
            base = f"search_tiles_{datetime.now():%Y%m%d_%H%M%S}"
            with open(os.path.join(settings.debug_dir, f"{base}.html"), "w", encoding="utf-8") as f:
                f.write(raw["html"])
            files["html"] = f"{base}.html"
            if raw.get("screenshot_png"):
                with open(os.path.join(settings.debug_dir, f"{base}.png"), "wb") as f:
                    f.write(raw["screenshot_png"])
                files["png"] = f"{base}.png"

        logger.info("search_tiles: query=%r плиток на странице=%s, взято=%s, прокруток=%s",
                    query, len(raw["raw_tiles"]), len(tiles), raw["scrolls"])
        return {"query": query, "url": raw["url"], "scrolls": raw["scrolls"],
                "tiles_total": len(tiles), "tiles": tiles, "debug_files": files}

    def select_for_stratum(self, req) -> dict:
        """
        Подбор карточек под страту. Категорийная логика — в стратегии
        (app/categories), оркестрация — в selection.select_cards.

        Два браузера на задачу (см. _SelectBrowsers): выдача — в браузере выдачи
        подбора (открыта всю задачу, кандидаты читаются с отрисованных плиток,
        догрузка — прокруткой), карточки-кандидаты — в браузере карточек подбора.
        Капча после набранного — отдаётся набранное (blocked в результате).
        Сессии обхода и выдачи подбор не трогает.
        """
        from app.categories.registry import get_strategy

        strategy = get_strategy(getattr(req, "category", None))
        min_rating, min_reviews = strategy.thresholds(self.policy)

        logger.info("Подбор для страты: категория=%s query=%r count=%s seasonal=%s "
                    "base_share=%s исключений=%s пороги=(%s, %s)",
                    strategy.name, req.query, req.count, req.is_seasonal,
                    req.base_share, len(req.exclude), min_rating, min_reviews)

        # Пауза между переходами внутри задачи — та же, что между карточками обхода.
        pacer = NavigationPacer(self.policy.between_cards_min,
                                self.policy.between_cards_max, sleep=self._sleep)
        browsers = _SelectBrowsers(self)
        search_url = OzonRepository.search_url_from_query(req.query)
        listing = {"opened": False}

        def load_candidates(known, want):
            # Выдача одна, догружается прокруткой: первый раз - переход на поиск
            # изнутри Ozon, дальше - прокрутка той же выдачи до want новых плиток.
            parser = browsers.get(SELECT_LISTING)
            try:
                if not listing["opened"]:
                    pacer.wait()
                    try:
                        parser.fetch_listing_scrolled(search_url, 0)
                    finally:
                        parser.consume_session_step()
                    listing["opened"] = True
                raw_tiles, _ = parser.scroll_new_tiles(known, want, self.policy.search_max_scrolls)
            except AntibotBlockedError as exc:
                browsers.blocked(SELECT_LISTING, parser, exc, search_url)
                raise
            except WebDriverException as e:
                raise browsers.failure(SELECT_LISTING, parser, e, search_url) from e
            tiles = [t for t in parse_tiles(raw_tiles, len(raw_tiles)) if t.sku not in known]
            return candidates_from_tiles(tiles)

        def get_card_only(card_url):
            pacer.wait()
            t0 = time.monotonic()
            parser = browsers.get(SELECT_CARDS)
            try:
                html = parser.fetch_html_in_session(card_url, full_content=True)
            except OutOfStockError:
                raise                       # selection пропустит кандидата
            except AntibotBlockedError as exc:
                browsers.blocked(SELECT_CARDS, parser, exc, card_url)
                self._emit_blocked(card_url, exc)
                raise
            except WebDriverException as e:
                failure = browsers.failure(SELECT_CARDS, parser, e, card_url)
                self._emit_blocked(card_url, failure)
                raise failure from e
            finally:
                parser.consume_session_step()
            not_found = ('data-widget="error"' in html
                         or "Такой страницы не существует" in html)
            nuxt = {} if not_found else extract_nuxt_state(html)
            card = build_card(html, nuxt, card_url)
            raw = {"html": html, "nuxt_state": nuxt, "url": card_url,
                   "screenshot_png": parser.screenshot_png() if settings.debug else None}
            self._save_debug(self._basename(card_url), raw, card.model_dump())
            emit_event("ozon.card.parsed", {"url": card_url, "sku": card.sku,
                                            "took_s": round(time.monotonic() - t0, 2)})
            return card

        with self._lock:
            try:
                return select_cards(
                    req,
                    load_candidates=load_candidates,
                    get_card=get_card_only,
                    min_rating=min_rating,
                    min_reviews=min_reviews,
                    max_loads=self.policy.max_pages,
                    strategy=strategy,
                    pool_multiplier=self.policy.pool_multiplier,
                    min_pool=self.policy.search_tiles_target,
                )
            finally:
                browsers.close_all()


class _SelectBrowsers:
    """Браузеры одной задачи подбора: выдачи (SELECT_LISTING) и карточек (SELECT_CARDS).

    У каждого свой постоянный профиль (OZON_SELECT_LISTING_PROFILE_DIR,
    OZON_SELECT_CARDS_PROFILE_DIR; не задан — временный): кука с главной копится
    между задачами. Браузер начинает сессию с главной Ozon (begin_session) и дальше
    переходит изнутри Ozon; исчерпан лимит переходов — новый браузер, снова с главной.
    Капча — браузер закрывается, его профиль очищается (следующий начнёт с чистого);
    сбой загрузки — браузер закрывается, профиль остаётся. В конце задачи — close_all.
    """

    def __init__(self, service: "OzonService"):
        self._service = service
        self._parsers = {SELECT_LISTING: None, SELECT_CARDS: None}

    @staticmethod
    def profile_dir(kind: str) -> str:
        return os.environ.get(_SELECT_PROFILE_ENV[kind]) or ""

    def get(self, kind: str):
        """Живой браузер с неисчерпанным лимитом переходов; нет — новый, с главной Ozon."""
        parser = self._parsers[kind]
        if parser is not None and not parser.session_exhausted():
            return parser
        self.close(kind)
        logger.info("Подбор: новый браузер %s", _SELECT_TITLE[kind])
        parser = OzonParser(self._service.policy, profile_dir=self.profile_dir(kind))
        self._parsers[kind] = parser
        parser.begin_session()
        return parser

    def close(self, kind: str) -> None:
        parser = self._parsers[kind]
        if parser is not None:
            try:
                parser.close()
            except Exception as e:
                logger.debug("Ошибка закрытия браузера %s: %s", _SELECT_TITLE[kind], e)
            self._parsers[kind] = None

    def close_all(self) -> None:
        for kind in self._parsers:
            self.close(kind)

    def blocked(self, kind: str, parser, exc: AntibotBlockedError, url: str) -> None:
        """Снимок блокировки, закрытие браузера; при капче — очистка его профиля."""
        self._service._save_block_artifacts(parser, exc, url, session=kind)
        self.close(kind)
        if not isinstance(exc, PageLoadError) and _wipe_profile(self.profile_dir(kind)):
            logger.warning("Профиль браузера %s очищен после капчи: %s",
                           _SELECT_TITLE[kind], self.profile_dir(kind))

    def failure(self, kind: str, parser, e: WebDriverException, url: str) -> PageLoadError:
        """Браузер не отвечает — сбой загрузки: снимок, закрытие, профиль остаётся."""
        first_line = (getattr(e, "msg", None) or type(e).__name__).splitlines()[0][:200]
        failure = PageLoadError(f"Браузер не отвечает: {first_line}", marketplace="ozon")
        self.blocked(kind, parser, failure, url)
        return failure