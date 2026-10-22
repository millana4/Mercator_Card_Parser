# -*- coding: utf-8 -*-
"""Подбор — новым браузером с временным профилем на каждую страницу (repository),
долгоживущие сессии не трогает."""
import pytest

import app.marketplaces.ozon.repository as repository_module
import app.marketplaces.ozon.service as service_module
from app.categories.models import StratumRequest
from app.core.config import settings
from app.marketplaces.ozon.service import OzonService

PAGE = "<html><body>__NUXT__</body></html>"


class FakeRepo:
    def __init__(self):
        self.calls = []

    def fetch_search_raw(self, query=None, url=None):
        self.calls.append(("page", query, url))
        return {"html": PAGE, "url": "u", "next_page": None, "current_page": 1,
                "total_pages": 1, "screenshot_png": None}

    def fetch_raw(self, url, full_content=False):
        self.calls.append(("card", url, full_content))
        return {"html": PAGE, "nuxt_state": {}, "screenshot_png": None, "url": url,
                "not_found": False}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    s = OzonService(use_session=True)
    s.repo = FakeRepo()
    s._sleep = lambda seconds: None
    monkeypatch.setattr(service_module, "OzonParser",
                        lambda *a, **k: pytest.fail("подбор не должен открывать сессию"))
    return s


def test_select_uses_fresh_browsers(service, monkeypatch):
    def fake_select_cards(req, fetch_page, get_card, **kwargs):
        fetch_page(None)
        fetch_page("https://www.ozon.ru/search/?text=x&page=2")
        get_card("https://www.ozon.ru/product/1/")
        return {"cards": [], "requested": req.count, "found": 0}

    monkeypatch.setattr(service_module, "select_cards", fake_select_cards)
    req = StratumRequest(query="жакет женский", count=1, is_seasonal=False,
                         base_share=None, exclude=[], category="clothing")
    service.select_for_stratum(req)

    assert service.repo.calls == [
        ("page", "жакет женский", None),
        ("page", None, "https://www.ozon.ru/search/?text=x&page=2"),
        ("card", "https://www.ozon.ru/product/1/", True),
    ]
    assert service._search_parser is None and service._crawl_parser is None


def test_repository_uses_temporary_profile(monkeypatch):
    created = []

    class FakeParser:
        def __init__(self, policy, device=None, profile_dir=None):
            created.append(profile_dir)

        def fetch_html(self, url, full_content=False):
            return PAGE

        def fetch_listing_html(self, url):
            return PAGE

        def screenshot_png(self):
            return None

        def close(self):
            pass

    monkeypatch.setattr(repository_module, "OzonParser", FakeParser)
    monkeypatch.setattr(repository_module, "extract_nuxt_state", lambda html: {})
    monkeypatch.setenv("OZON_CHROME_PROFILE_DIR", "/app/chrome-profile")
    repo = repository_module.OzonRepository(settings.ozon_policy())
    repo.fetch_raw("https://www.ozon.ru/product/1/")
    repo.fetch_search_raw(query="жакет")
    assert created == ["", ""]       # временный профиль, не профиль обхода