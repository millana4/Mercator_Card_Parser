# -*- coding: utf-8 -*-
"""Подбор: два своих браузера на задачу — выдачи и карточек, со своими постоянными
профилями. Каждый начинает с главной Ozon (begin_session) и переходит изнутри Ozon;
выдача и карточки не смешиваются; сессии обхода и выдачи подбор не трогает.
Выдача открывается один раз и догружается прокруткой, кандидаты — с отрисованных плиток.
Капча — профиль поймавшего браузера очищается; сбой загрузки — профиль остаётся.
Капча после набранного — обработчик отдаёт набранное с признаком блокировки."""
import os
from types import SimpleNamespace

import pytest

import app.marketplaces.ozon.repository as repository_module
import app.marketplaces.ozon.service as service_module
from app.categories.models import StratumRequest
from app.core.config import settings
from app.core.exceptions import AntibotBlockedError, PageLoadError
from app.marketplaces.ozon.service import OzonService
from app.worker.handlers import handle_select

PAGE = "<html><body>__NUXT__</body></html>"
SEARCH_URL = ("https://www.ozon.ru/search/?text="
              "%D1%8E%D0%B1%D0%BA%D0%B0+%D0%B4%D0%BB%D1%8F+%D0%B4%D0%B5%D0%B2%D0%BE%D1%87%D0%BA%D0%B8")
RAW_TILES = [
    {"href": "/product/yubka-krasnaya-111/?at=x", "texts": ["500 ₽", "Юбка красная", "4.9", "120 отзывов"]},
    {"href": "/product/yubka-sinyaya-222/", "texts": ["700 ₽", "Юбка синяя", "4.8", "45 отзывов"]},
]


class FakeParser:
    """Браузер без Chrome: журнал действий, лимит сессии, капча по требованию."""
    created = []
    session_limit = 30
    fail_on_card = None          # исключение при открытии карточки

    def __init__(self, policy, device=None, profile_dir=None):
        self.profile_dir = profile_dir
        self.actions = []
        self.closed = False
        self._session_left = 0
        FakeParser.created.append(self)

    def begin_session(self):
        self._session_left = FakeParser.session_limit
        self.actions.append("home")

    def session_exhausted(self):
        return self._session_left <= 0

    def consume_session_step(self):
        self._session_left -= 1

    def fetch_listing_scrolled(self, url, steps):
        self.actions.append(("listing", url, steps))
        return PAGE

    def scroll_new_tiles(self, known, want, max_scrolls):
        self.actions.append(("scroll", want))
        return RAW_TILES, 1

    def fetch_html_in_session(self, url, full_content=False):
        if FakeParser.fail_on_card is not None:
            raise FakeParser.fail_on_card
        self.actions.append(("card", url, full_content))
        return PAGE

    def screenshot_png(self):
        return None

    def block_snapshot(self):
        return {"title": "Antibot Challenge Page", "url": "https://www.ozon.ru/x", "png": None}

    def close(self):
        self.closed = True


@pytest.fixture
def env(monkeypatch, tmp_path):
    listing_dir, cards_dir = tmp_path / "listing", tmp_path / "cards"
    for d in (listing_dir, cards_dir):
        d.mkdir()
        (d / "Cookies").write_text("x")
    monkeypatch.setenv("OZON_SELECT_LISTING_PROFILE_DIR", str(listing_dir))
    monkeypatch.setenv("OZON_SELECT_CARDS_PROFILE_DIR", str(cards_dir))
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "debug_dir", str(tmp_path / "debug"))
    monkeypatch.setattr(service_module, "OzonParser", FakeParser)
    monkeypatch.setattr(service_module, "extract_nuxt_state", lambda html: {})
    monkeypatch.setattr(service_module, "build_card",
                        lambda html, nuxt, url: SimpleNamespace(sku=url, model_dump=lambda: {"url": url}))
    FakeParser.created, FakeParser.session_limit, FakeParser.fail_on_card = [], 30, None
    service = OzonService(use_session=True)
    service._sleep = lambda seconds: None
    return service, listing_dir, cards_dir


def _run(service, monkeypatch, steps):
    """Подбор с подменённой оркестрацией. steps: [("load", known, want) | ("card", url)].
    Возвращает списки кандидатов, отданных каждой догрузкой."""
    loaded = []

    def fake_select_cards(req, load_candidates, get_card, **kwargs):
        for step in steps:
            if step[0] == "load":
                loaded.append(load_candidates(step[1], step[2]))
            else:
                get_card(step[1])
        return {"cards": [], "requested": req.count, "found": 0, "blocked": None}

    monkeypatch.setattr(service_module, "select_cards", fake_select_cards)
    req = StratumRequest(query="юбка для девочки", count=3, is_seasonal=False,
                         base_share=None, exclude=[], category="kids_clothing")
    service.select_for_stratum(req)
    return loaded


def test_listing_opened_once_and_scrolled_cards_in_own_browser(env, monkeypatch):
    service, listing_dir, cards_dir = env
    _run(service, monkeypatch, [("load", set(), 32), ("card", "c1"), ("load", {"111"}, 30), ("card", "c2")])

    listing, cards = FakeParser.created
    assert listing.profile_dir == str(listing_dir) and cards.profile_dir == str(cards_dir)
    # выдача открыта один раз, дальше - только прокрутка той же выдачи
    assert listing.actions == ["home", ("listing", SEARCH_URL, 0), ("scroll", 32), ("scroll", 30)]
    assert cards.actions == ["home", ("card", "c1", True), ("card", "c2", True)]
    assert listing.closed and cards.closed                     # в конце задачи
    assert service._crawl_parser is None and service._search_parser is None
    assert os.listdir(listing_dir) == ["Cookies"] and os.listdir(cards_dir) == ["Cookies"]


def test_candidates_read_from_rendered_tiles_without_known(env, monkeypatch):
    service, _, _ = env
    first, second = _run(service, monkeypatch, [("load", set(), 32), ("load", {"111"}, 32)])

    assert [(c.sku, c.rating, c.reviews) for c in first] == [("111", 4.9, 120), ("222", 4.8, 45)]
    assert first[0].url == "https://www.ozon.ru/product/yubka-krasnaya-111/"
    assert [c.sku for c in second] == ["222"]                  # уже рассмотренные не повторяются


def test_cards_browser_recreated_when_session_exhausted(env, monkeypatch):
    service, _, _ = env
    FakeParser.session_limit = 2
    _run(service, monkeypatch, [("load", set(), 32), ("card", "c1"), ("card", "c2"), ("card", "c3")])

    cards = FakeParser.created[1:]
    assert len(cards) == 2
    assert cards[0].actions == ["home", ("card", "c1", True), ("card", "c2", True)]
    assert cards[1].actions == ["home", ("card", "c3", True)]   # новый браузер — снова с главной
    assert all(p.closed for p in FakeParser.created)


def test_captcha_wipes_only_profile_of_its_browser(env, monkeypatch):
    service, listing_dir, cards_dir = env
    FakeParser.fail_on_card = AntibotBlockedError("капча", marketplace="ozon")
    with pytest.raises(AntibotBlockedError):
        _run(service, monkeypatch, [("load", set(), 32), ("card", "c1")])

    assert os.listdir(cards_dir) == []                         # профиль карточек очищен
    assert os.listdir(listing_dir) == ["Cookies"]              # профиль выдачи цел
    assert all(p.closed for p in FakeParser.created)
    blocks = [n for n in os.listdir(settings.debug_dir) if n.endswith(".json")]
    assert len(blocks) == 1


def test_page_load_error_keeps_profile(env, monkeypatch):
    service, _, cards_dir = env
    FakeParser.fail_on_card = PageLoadError("нет __NUXT__", marketplace="ozon")
    with pytest.raises(PageLoadError):
        _run(service, monkeypatch, [("load", set(), 32), ("card", "c1")])
    assert os.listdir(cards_dir) == ["Cookies"]


def test_temporary_profiles_without_env(env, monkeypatch):
    service, _, _ = env
    monkeypatch.delenv("OZON_SELECT_LISTING_PROFILE_DIR")
    monkeypatch.delenv("OZON_SELECT_CARDS_PROFILE_DIR")
    _run(service, monkeypatch, [("load", set(), 32), ("card", "c1")])
    assert [p.profile_dir for p in FakeParser.created] == ["", ""]


def test_select_pauses_between_navigations(env, monkeypatch):
    service, _, _ = env
    sleeps = []
    service._sleep = sleeps.append
    _run(service, monkeypatch, [("load", set(), 32), ("card", "a"), ("card", "b")])
    assert len(sleeps) == 2                                    # перед первым переходом — без паузы
    lo, hi = service.policy.between_cards_min, service.policy.between_cards_max
    assert all(lo <= d <= hi for d in sleeps)


def test_handler_returns_partial_with_ban_mark():
    """Капча после набранного: ядру - набранное (ok), воркеру - признак бана для паузы."""
    class Service:
        def select_for_stratum(self, req):
            return {"cards": [{"sku": "1"}], "requested": 3, "found": 1,
                    "blocked": AntibotBlockedError("капча", marketplace="ozon")}

    r = handle_select({"task_id": "t", "query": "юбка для девочки", "count": 3}, Service())
    assert r["ok"] is True and r["found_count"] == 1 and r["cards"] == [{"sku": "1"}]
    assert r["_failure"] == "ban"


def test_repository_uses_temporary_profile(monkeypatch):
    """Репозиторий (диагностика без сессии) по-прежнему на временном профиле."""
    created = []

    class RepoParser:
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

    monkeypatch.setattr(repository_module, "OzonParser", RepoParser)
    monkeypatch.setattr(repository_module, "extract_nuxt_state", lambda html: {})
    monkeypatch.setenv("OZON_CHROME_PROFILE_DIR", "/app/chrome-profile")
    repo = repository_module.OzonRepository(settings.ozon_policy())
    repo.fetch_raw("https://www.ozon.ru/product/1/")
    repo.fetch_search_raw(query="жакет")
    assert created == ["", ""]