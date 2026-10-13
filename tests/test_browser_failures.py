# -*- coding: utf-8 -*-
"""Ошибка браузера в пути сессии — сбой загрузки: снимок, закрытие сессии."""
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
    return s


def test_listing_timeout_is_page_load_failure(service, tmp_path):
    parser = service._crawl_parser
    with pytest.raises(PageLoadError) as e:
        service._listing_in_session("https://www.ozon.ru/search/?text=x", 0)
    assert _failure_kind(e.value) == "load"
    assert parser.closed and parser.steps == 1
    assert service._crawl_parser is None
    assert any(p.name.endswith(".json") for p in tmp_path.iterdir())


def test_crawl_timeout_is_page_load_failure(service):
    parser = service._crawl_parser
    with pytest.raises(PageLoadError):
        service.get_card_for_crawl("https://www.ozon.ru/product/1/")
    assert parser.closed
    assert service._crawl_parser is None