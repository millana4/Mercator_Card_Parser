# -*- coding: utf-8 -*-
"""Две долгоживущие сессии браузера: обход и выдача.
Свои браузеры, профили и поколения устройств; сброс и закрытие — по сессии."""
import json
import os

import pytest

from app.core.config import settings
from app.core.exceptions import AntibotBlockedError
from app.marketplaces.ozon import service as service_module
from app.marketplaces.ozon.service import CRAWL, SEARCH, OzonService

PAGE = "<html><body>__NUXT__</body></html>"


class FakeParser:
    """Парсер без браузера: запоминает профиль и устройство, с которыми создан."""
    created = []

    def __init__(self, policy, device=None, profile_dir=None):
        self.device = device
        self.profile_dir = profile_dir
        self.closed = False
        self._session_left = 30
        self.driver = type("Driver", (), {"page_source": PAGE})()
        FakeParser.created.append(self)

    def begin_session(self):
        pass

    def session_exhausted(self):
        return self._session_left <= 0

    def consume_session_step(self):
        self._session_left -= 1

    def fetch_html_in_session(self, url, full_content=False):
        return PAGE

    def fetch_listing_scrolled(self, url, steps):
        return PAGE

    def scroll_collect_tiles(self, target, max_scrolls):
        return [], 0

    def screenshot_png(self):
        return None

    def block_snapshot(self):
        return {"title": "Antibot Captcha", "url": "https://www.ozon.ru/x", "png": None}

    def close(self):
        self.closed = True

    @staticmethod
    def extract_nuxt_state(html):
        return {}

    @staticmethod
    def _sku_from_url(url):
        return "1"


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    crawl_dir, search_dir = tmp_path / "crawl", tmp_path / "search"
    for d in (crawl_dir, search_dir):
        d.mkdir()
        (d / "Cookies").write_text("x")
    monkeypatch.setenv("OZON_CHROME_PROFILE_DIR", str(crawl_dir))
    monkeypatch.setenv("OZON_SEARCH_PROFILE_DIR", str(search_dir))
    monkeypatch.setenv("PARSER_ID", "14")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "debug_dir", str(tmp_path / "debug"))
    monkeypatch.setattr(service_module, "OzonParser", FakeParser)
    monkeypatch.setattr(service_module, "extract_nuxt_state", lambda html: {})
    FakeParser.created = []
    return crawl_dir, search_dir


def test_crawl_and_search_use_separate_browsers(dirs):
    crawl_dir, search_dir = dirs
    s = OzonService(use_session=True)
    s.get_card_for_crawl_by_id("5")
    s.search_tiles("брюки", save_files=False)
    s.get_card("https://www.ozon.ru/product/7/")      # карточка из выдачи — та же сессия выдачи
    assert len(FakeParser.created) == 2
    assert s._crawl_parser.profile_dir == str(crawl_dir)
    assert s._search_parser.profile_dir == str(search_dir)


def test_search_profile_temporary_without_env(dirs, monkeypatch):
    monkeypatch.delenv("OZON_SEARCH_PROFILE_DIR")
    s = OzonService(use_session=True)
    s.search_tiles("брюки", save_files=False)
    assert s._search_parser.profile_dir == ""       # временный, не профиль обхода


def test_reset_profile_only_own_session(dirs):
    crawl_dir, search_dir = dirs
    s = OzonService(use_session=True)
    s.get_card_for_crawl_by_id("5")
    s.search_tiles("брюки", save_files=False)
    crawl = s._crawl_parser
    s.reset_profile(SEARCH)
    assert s._search_parser is None
    assert os.listdir(search_dir) == []
    assert s._crawl_parser is crawl and not crawl.closed
    assert os.listdir(crawl_dir) == ["Cookies"]


def test_reset_profile_all_sessions(dirs):
    crawl_dir, search_dir = dirs
    s = OzonService(use_session=True)
    s.get_card_for_crawl_by_id("5")
    s.reset_profile()
    assert s._crawl_parser is None
    assert os.listdir(crawl_dir) == [] and os.listdir(search_dir) == []


def test_device_generation_per_session(dirs):
    s = OzonService(use_session=True)
    assert s._device(CRAWL) == s._device(SEARCH)    # при старте — одно устройство
    s.set_device_generation(3, SEARCH)
    assert s._device(SEARCH) != s._device(CRAWL)
    s.search_tiles("брюки", save_files=False)
    assert s._search_parser.device == s._device(SEARCH)
    s.set_device_generation(5)                      # без сессии — обе
    assert s._device(CRAWL) == s._device(SEARCH)


def test_close_crawl_closes_both(dirs):
    s = OzonService(use_session=True)
    s.get_card_for_crawl_by_id("5")
    s.search_tiles("брюки", save_files=False)
    crawl, search = s._crawl_parser, s._search_parser
    s.close_crawl()
    assert crawl.closed and search.closed
    assert s._crawl_parser is None and s._search_parser is None


def test_block_closes_session_and_records_it(dirs, tmp_path):
    s = OzonService(use_session=True)
    s.search_tiles("брюки", save_files=False)
    parser = s._search_parser
    s._close_after_block(SEARCH, parser, AntibotBlockedError("капча"), "https://www.ozon.ru/x")
    assert parser.closed and s._search_parser is None
    infos = [json.loads(p.read_text(encoding="utf-8"))
             for p in (tmp_path / "debug").iterdir() if p.name.endswith(".json")]
    assert [(i["kind"], i["session"]) for i in infos] == [("ban", SEARCH)]


def test_exhausted_session_recreated(dirs):
    s = OzonService(use_session=True)
    s.search_tiles("брюки", save_files=False)
    first = s._search_parser
    first._session_left = 0
    s.search_tiles("брюки", save_files=False)
    assert first.closed and s._search_parser is not first