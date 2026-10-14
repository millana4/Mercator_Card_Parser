# -*- coding: utf-8 -*-
"""Ошибка браузера в пути сессии — сбой загрузки: снимок, закрытие сессии.
Закрывается только сессия, где случился сбой; другая не трогается."""
import json

import pytest
from selenium.common.exceptions import TimeoutException

from app.core.config import settings
from app.core.exceptions import PageLoadError
from app.marketplaces.ozon.service import OzonService
from app.worker.handlers import _failure_kind


class HangingParser:
    """Парсер, у которого рендерер не отвечает на любой переход."""

    def __init__(self):
        self.closed = False
        self.steps = 0

    def session_exhausted(self):
        return False

    def consume_session_step(self):
        self.steps += 1

    def fetch_listing_scrolled(self, url, steps):
        raise TimeoutException("timeout: Timed out receiving message from renderer: 30.000")

    def fetch_html_in_session(self, url, full_content=False):
        raise TimeoutException("timeout: Timed out receiving message from renderer: 30.000")

    def block_snapshot(self):
        return {"title": "New Tab", "url": "chrome://new-tab-page/", "png": None}

    def close(self):
        self.closed = True


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "debug_dir", str(tmp_path))
    s = OzonService(use_session=True)
    s._crawl_parser = HangingParser()
    s._search_parser = HangingParser()
    return s


def _block_infos(tmp_path):
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(tmp_path.iterdir()) if p.name.endswith(".json")]


def test_listing_timeout_is_page_load_failure(service, tmp_path):
    search, crawl = service._search_parser, service._crawl_parser
    with pytest.raises(PageLoadError) as e:
        service._listing_in_session("https://www.ozon.ru/search/?text=x", 0)
    assert _failure_kind(e.value) == "load"
    assert search.closed and search.steps == 1
    assert service._search_parser is None
    assert service._crawl_parser is crawl and not crawl.closed
    assert [i["session"] for i in _block_infos(tmp_path)] == ["search"]


def test_crawl_timeout_is_page_load_failure(service, tmp_path):
    search, crawl = service._search_parser, service._crawl_parser
    with pytest.raises(PageLoadError):
        service.get_card_for_crawl("https://www.ozon.ru/product/1/")
    assert crawl.closed
    assert service._crawl_parser is None
    assert service._search_parser is search and not search.closed
    assert [i["session"] for i in _block_infos(tmp_path)] == ["crawl"]