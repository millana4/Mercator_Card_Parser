# -*- coding: utf-8 -*-
"""
test_full_content.py — ожидание полного блока характеристик и передача признака
полного разбора по цепочке вызовов.

Без браузера: драйвер, репозиторий и парсер сессии подменены, часы — поддельные.

Запуск:  python -m pytest tests/test_full_content.py -v
"""

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.marketplaces.ozon import parser as parser_module
from app.marketplaces.ozon.parser import OzonParser, FULL_CONTENT_MARKER
from app.marketplaces.ozon.service import OzonService
from app.marketplaces.ozon import service as service_module
from app.worker.handlers import handle_parse

URL = "https://www.ozon.ru/product/4569427009/"
EMPTY_PAGE = "<html><body>__NUXT__</body></html>"
FULL_PAGE = f"<html><body>__NUXT__<div {FULL_CONTENT_MARKER}></div></body></html>"
NOT_FOUND_PAGE = '<html><body>__NUXT__<div data-widget="error"></div></body></html>'


# ---------- Подмены ----------

class FakeClock:
    """Часы, которые идут только во время sleep."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeDriver:
    """page_source становится полным после appear_after прокруток; None — никогда."""

    def __init__(self, appear_after=None, page=EMPTY_PAGE):
        self.appear_after = appear_after
        self.page = page
        self.scrolls = 0

    @property
    def page_source(self):
        if self.appear_after is not None and self.scrolls >= self.appear_after:
            return FULL_PAGE
        return self.page

    def execute_script(self, script, *args):
        self.scrolls += 1


class FakeRepo:
    """Репозиторий без браузера: запоминает признак полного разбора."""

    def __init__(self):
        self.calls = []

    def fetch_raw(self, url, full_content=False):
        self.calls.append(full_content)
        return {"html": EMPTY_PAGE, "nuxt_state": {}, "screenshot_png": None,
                "url": url, "not_found": False}


class FakeSessionParser:
    """Парсер долгоживущей сессии без браузера: запоминает признак полного разбора."""

    def __init__(self):
        self.calls = []
        self._session_left = 10

    def session_exhausted(self):
        return False

    def fetch_html_in_session(self, url, full_content=False):
        self.calls.append(full_content)
        return EMPTY_PAGE

    def consume_session_step(self):
        self._session_left -= 1

    def screenshot_png(self):
        return None

    @staticmethod
    def extract_nuxt_state(html):
        return {}


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(parser_module, "time",
                        SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep))
    return fake


@pytest.fixture
def no_debug(monkeypatch):
    # Без записи debug-файлов на диск
    monkeypatch.setattr(settings, "debug", False)


def _parser(driver, timeout=15.0):
    parser = OzonParser.__new__(OzonParser)  # без запуска Chrome
    parser.policy = SimpleNamespace(full_content_timeout=timeout)
    parser.driver = driver
    return parser


# ---------- Ожидание полного блока ----------

def test_block_already_present_no_scroll(clock):
    driver = FakeDriver(appear_after=0)
    assert _parser(driver).ensure_full_content() is True
    assert driver.scrolls == 0


def test_block_appears_after_scrolls(clock):
    driver = FakeDriver(appear_after=3)
    assert _parser(driver).ensure_full_content() is True
    assert driver.scrolls == 3


def test_block_never_appears_stops_on_timeout(clock):
    driver = FakeDriver()
    assert _parser(driver).ensure_full_content() is False
    assert 15.0 <= clock.now < 17.0
    assert 8 <= driver.scrolls <= 15


def test_not_found_page_no_wait(clock):
    driver = FakeDriver(page=NOT_FOUND_PAGE)
    assert _parser(driver).ensure_full_content() is False
    assert driver.scrolls == 0


# ---------- Признак полного разбора по цепочке ----------

def test_get_card_requests_full_content(no_debug):
    service = OzonService(use_session=False)
    service.repo = FakeRepo()
    service.get_card(URL)
    assert service.repo.calls == [True]


def test_get_card_in_session_requests_full_content(no_debug, monkeypatch):
    # В режиме сессии __NUXT__ разбирает функция модуля service — подменяем её.
    # get_card (подбор, HTTP) идёт в сессии выдачи.
    monkeypatch.setattr(service_module, "extract_nuxt_state", lambda html: {})
    service = OzonService(use_session=True)
    service._search_parser = FakeSessionParser()
    service.get_card(URL)
    assert service._search_parser.calls == [True]


def test_get_raw_without_full_content(no_debug):
    service = OzonService(use_session=False)
    service.repo = FakeRepo()
    service.get_raw(URL)
    assert service.repo.calls == [False]


def test_crawl_default_without_full_content(no_debug):
    service = OzonService(use_session=False)
    service._crawl_parser = FakeSessionParser()
    service.get_card_for_crawl(URL)
    assert service._crawl_parser.calls == [False]


def test_crawl_with_full_content_flag(no_debug):
    service = OzonService(use_session=False)
    service._crawl_parser = FakeSessionParser()
    service.get_card_for_crawl_by_id("4569427009", full_content=True)
    assert service._crawl_parser.calls == [True]




# ---------- Пометка в задаче обхода ----------

class FakeCrawlService:
    """Сервис без браузера: запоминает sku и признак полного разбора."""

    def __init__(self):
        self.calls = []

    def get_card_for_crawl_by_id(self, sku, full_content=False):
        self.calls.append((sku, full_content))
        return {"card": {"sku": sku, "full_content": full_content}, "debug_files": None}


def test_parse_task_without_mark():
    service = FakeCrawlService()
    result = handle_parse({"task_id": "t1", "sku": "4569427009"}, service)
    assert result["ok"] is True
    assert service.calls == [("4569427009", False)]


def test_parse_task_with_full_content_mark():
    service = FakeCrawlService()
    result = handle_parse({"task_id": "t2", "sku": "4569427009", "full_content": True}, service)
    assert result["ok"] is True
    assert service.calls == [("4569427009", True)]


def test_parse_task_mark_only_strict_true():
    service = FakeCrawlService()
    handle_parse({"task_id": "t3", "sku": "1", "full_content": False}, service)
    handle_parse({"task_id": "t4", "sku": "2", "full_content": "false"}, service)
    assert service.calls == [("1", False), ("2", False)]