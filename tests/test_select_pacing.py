# -*- coding: utf-8 -*-
"""Пауза между переходами внутри задачи подбора (одна сессия браузера на все задачи)."""
import app.marketplaces.ozon.service as service_module
from app.categories.models import StratumRequest
from app.marketplaces.ozon.service import NavigationPacer, OzonService


def test_pacer_first_wait_without_pause():
    calls = []
    pacer = NavigationPacer(50.0, 90.0, sleep=calls.append)
    pacer.wait()
    assert calls == []


def test_pacer_next_waits_in_range():
    calls = []
    pacer = NavigationPacer(50.0, 90.0, sleep=calls.append)
    for _ in range(4):
        pacer.wait()
    assert len(calls) == 3
    assert all(50.0 <= d <= 90.0 for d in calls)


def test_select_pauses_between_navigations(monkeypatch):
    service = OzonService(use_session=True)
    sleeps = []
    service._sleep = sleeps.append
    navigations = []

    def fake_fetch_listing(query=None, url=None):
        navigations.append(("page", query, url))
        return {"html": "", "url": "u"}

    def fake_get_card(card_url, full_content=True):
        navigations.append(("card", card_url))
        return {"card": {"sku": card_url}}

    def fake_select_cards(req, fetch_page, get_card, **kwargs):
        fetch_page(None)
        get_card("a")
        get_card("b")
        return {"cards": [], "requested": req.count, "found": 0}

    monkeypatch.setattr(service, "_fetch_listing", fake_fetch_listing)
    monkeypatch.setattr(service, "get_card", fake_get_card)
    monkeypatch.setattr(service_module, "select_cards", fake_select_cards)
    monkeypatch.setattr(service_module, "OzonCard", lambda **kw: kw)

    req = StratumRequest(query="лонгслив", count=2, is_seasonal=False,
                         base_share=None, exclude=[], category="kids_clothing")
    service.select_for_stratum(req)

    assert navigations == [("page", "лонгслив", None), ("card", "a"), ("card", "b")]
    assert len(sleeps) == 2
    lo, hi = service.policy.between_cards_min, service.policy.between_cards_max
    assert all(lo <= d <= hi for d in sleeps)