# -*- coding: utf-8 -*-
"""
test_selection.py — тесты подбора карточек под страту.

Работают БЕЗ сети: выдача берётся из сохранённого HTML (фикстура),
заход в карточку и открытие страниц мокаются. Проверяют извлечение
кандидатов, первичный отсев, расчёт потребности, сезонный фильтр,
уникальность магазина и оркестратор набора с пагинацией.

Запуск: pytest tests/test_selection.py -v
"""

import os
from datetime import date

from app.marketplaces.ozon.search_listing import (
    extract_candidates, extract_next_page, prefilter, dedupe, passes_quality,
)
from app.marketplaces.ozon.selection import (
    Collection, ExcludedCard, StratumRequest,
    current_season, expected_season_year, classify_collection,
    compute_need, card_matches_slot, seller_is_free, select_cards,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "ozon_search_trusy_zhenskie.html")


def _html():
    return open(FIXTURE, encoding="utf-8").read()


# ------------------------------------------------------------------ #
#  Моки карточки для оркестратора (вместо реального get_card)         #
# ------------------------------------------------------------------ #
class _Seller:
    def __init__(self, sid):
        self.id = sid


class _Card:
    """Минимальная заглушка OzonCard: sku, seller.id, characteristics,
    плюс reviews_count/rating — финальная проверка порога в select_cards
    читает их с карточки. Даём заведомо проходные значения."""
    def __init__(self, sku, seller_id, collection=None):
        self.sku = sku
        self.seller = _Seller(seller_id)
        self.characteristics = {"Коллекция": collection} if collection else {}
        self.reviews_count = "500"   # > порога, чтобы карточка проходила
        self.rating = "4.9"          # >= порога


# =================================================================== #
#  Извлечение кандидатов из выдачи                                     #
# =================================================================== #
def test_extract_candidates_count():
    cands = extract_candidates(_html())
    assert len(cands) == 8
    assert all(c.sku for c in cands)
    assert all(c.url for c in cands)


def test_extract_candidates_types():
    cands = extract_candidates(_html())
    # цена и рейтинг — числа, отзывы — целое
    assert all(isinstance(c.price, float) for c in cands)
    assert all(isinstance(c.rating, float) for c in cands)
    assert all(isinstance(c.reviews, int) for c in cands)


def test_extract_original_flag():
    cands = extract_candidates(_html())
    # в этой выдаче есть карточки с пометкой "Оригинал"
    assert any(c.is_original for c in cands)


def test_extract_next_page():
    nxt = extract_next_page(_html())
    assert nxt is not None
    assert nxt.startswith("https://www.ozon.ru/")
    assert "&amp;" not in nxt   # HTML-сущности раскодированы


def test_empty_html_no_candidates():
    assert extract_candidates("<html>нет виджета</html>") == []
    assert extract_next_page("<html>нет паджинатора</html>") is None


# =================================================================== #
#  Первичный отсев                                                     #
# =================================================================== #
def test_dedupe_removes_variants():
    cands = extract_candidates(_html())
    d = dedupe(cands)
    # варианты одного товара (одинаковый slug) схлопываются
    assert len(d) < len(cands)
    slugs = [c.slug for c in d]
    assert len(slugs) == len(set(slugs))   # slug'и уникальны


def test_quality_threshold():
    cands = extract_candidates(_html())
    good = [c for c in cands if passes_quality(c, 4.8, 100)]
    # все прошедшие имеют рейтинг >= 4.8 и отзывов > 100
    assert all(c.rating >= 4.8 for c in good)
    assert all(c.reviews > 100 for c in good)
    # 4.7 не проходит
    assert not any(c.rating == 4.7 for c in good)


def test_prefilter_prioritizes_original():
    cands = extract_candidates(_html())
    pf = prefilter(cands, [], 4.8, 100)
    if any(c.is_original for c in pf):
        # первый оригинал стоит раньше первого не-оригинала
        first_non = next((i for i, c in enumerate(pf) if not c.is_original), len(pf))
        first_orig = next((i for i, c in enumerate(pf) if c.is_original), len(pf))
        assert first_orig < first_non


def test_prefilter_drops_excluded():
    cands = extract_candidates(_html())
    victim = cands[0].sku
    ex = [ExcludedCard(sku=victim)]
    pf = prefilter(cands, ex, 4.8, 100)
    assert victim not in [c.sku for c in pf]


# =================================================================== #
#  Сезон / год / коллекция                                            #
# =================================================================== #
def test_current_season():
    assert current_season(date(2026, 7, 1)) == Collection.SPRING_SUMMER
    assert current_season(date(2026, 1, 1)) == Collection.AUTUMN_WINTER
    assert current_season(date(2026, 3, 1)) == Collection.SPRING_SUMMER
    assert current_season(date(2026, 9, 1)) == Collection.AUTUMN_WINTER


def test_expected_year():
    assert expected_season_year(date(2026, 7, 1)) == 2026   # ВЛ
    assert expected_season_year(date(2026, 10, 1)) == 2027  # ОЗ конец года
    assert expected_season_year(date(2026, 1, 1)) == 2026   # ОЗ янв


def test_classify_collection():
    assert classify_collection("Весна-лето 2026") == (Collection.SPRING_SUMMER, 2026)
    assert classify_collection("Базовая коллекция") == (Collection.BASE, None)
    assert classify_collection("Демисезон") == (Collection.BASE, None)
    assert classify_collection("Лето")[0] == Collection.SPRING_SUMMER
    assert classify_collection("Осень-зима 2027") == (Collection.AUTUMN_WINTER, 2027)


def test_card_matches_slot_season():
    jul = date(2026, 7, 1)
    assert card_matches_slot("Весна-лето 2026", "season", jul) is True
    assert card_matches_slot("Весна-лето 2025", "season", jul) is False  # прошлогодняя
    assert card_matches_slot("Лето", "season", jul) is True              # без года
    assert card_matches_slot("Базовая коллекция", "season", jul) is False


def test_card_matches_slot_base():
    jul = date(2026, 7, 1)
    assert card_matches_slot("Базовая коллекция", "base", jul) is True
    assert card_matches_slot("Демисезон", "base", jul) is True
    assert card_matches_slot("Весна-лето 2026", "base", jul) is False


def test_seller_uniqueness():
    taken = set()
    assert seller_is_free("111", taken) is True
    taken.add("111")
    assert seller_is_free("111", taken) is False   # занят
    assert seller_is_free("222", taken) is True
    assert seller_is_free(None, taken) is False     # без id — не проходит


# =================================================================== #
#  Расчёт потребности                                                 #
# =================================================================== #
def test_need_non_seasonal():
    req = StratumRequest(query="x", count=4, is_seasonal=False,
                         exclude=[ExcludedCard(sku="a")])
    need = compute_need(req)
    assert need.total == 4


def test_need_seasonal_split():
    # count=4, доля 0.5 -> 2 база + 2 сезон
    req = StratumRequest(query="x", count=4, is_seasonal=True, base_share=0.5)
    need = compute_need(req, date(2026, 7, 1))
    assert (need.base, need.seasonal) == (2, 2)


def test_need_seasonal_round_half_up():
    # count=5, доля 0.5 -> 3 база (в пользу базы) + 2 сезон
    req = StratumRequest(query="x", count=5, is_seasonal=True, base_share=0.5)
    need = compute_need(req, date(2026, 7, 1))
    assert (need.base, need.seasonal) == (3, 2)


def test_need_excludes_by_type():
    # После правки: exclude больше не уменьшает потребность по типам.
    # count=4, доля 0.5 -> всегда 2 база + 2 сезон, независимо от exclude.
    req = StratumRequest(query="x", count=4, is_seasonal=True, base_share=0.5,
                         exclude=[ExcludedCard(sku="1", collection=Collection.BASE),
                                  ExcludedCard(sku="2", collection=Collection.BASE),
                                  ExcludedCard(sku="3", collection=Collection.SPRING_SUMMER)])
    need = compute_need(req, date(2026, 7, 1))
    assert (need.base, need.seasonal) == (2, 2)


def test_need_seasonal_wrong_season_excludes_ignored():
    # После правки exclude не влияет на потребность вообще — всегда (2, 2).
    req = StratumRequest(query="x", count=4, is_seasonal=True, base_share=0.5,
                         exclude=[ExcludedCard(sku="1", collection=Collection.BASE),
                                  ExcludedCard(sku="2", collection=Collection.BASE),
                                  ExcludedCard(sku="3", collection=Collection.SPRING_SUMMER)])
    need = compute_need(req, date(2026, 1, 1))
    assert (need.base, need.seasonal) == (2, 2)


# =================================================================== #
#  Оркестратор (на фикстуре выдачи + моках карточек)                  #
# =================================================================== #
def _page_once(html):
    """fetch_page, отдающий одну страницу без следующей."""
    def fetch(url):
        return {"html": html, "url": url or "page1", "next_page": None}
    return fetch


def test_select_non_seasonal_picks_count():
    html = _html()
    # каждой карточке — уникальный магазин
    def get_card(url):
        import re
        sku = re.search(r"-(\d+)/?$", url).group(1)
        return _Card(sku, "seller_" + sku[-3:])
    req = StratumRequest(query="Трусы", count=3, is_seasonal=False)
    r = select_cards(req, _page_once(html), get_card, 4.8, 100, 3, date(2026, 7, 1))
    assert r["found"] == 3
    assert len(r["cards"]) == 3


def test_select_stops_when_enough():
    html = _html()
    def get_card(url):
        import re
        sku = re.search(r"-(\d+)/?$", url).group(1)
        return _Card(sku, "seller_" + sku[-3:])
    req = StratumRequest(query="Трусы", count=2, is_seasonal=False)
    r = select_cards(req, _page_once(html), get_card, 4.8, 100, 3, date(2026, 7, 1))
    assert r["found"] == 2   # ровно 2, не больше


def test_select_seller_dedup_needs_next_page():
    html = _html()
    # все карточки — один магазин: с одной страницы наберём максимум 1
    def get_card(url):
        return _Card("x", "ONE_SHOP")
    # fetch_page с одной следующей страницей (та же выдача), затем конец
    calls = {"n": 0}
    def fetch(url):
        calls["n"] += 1
        nxt = "page2" if calls["n"] == 1 else None
        return {"html": html, "url": url or "page1", "next_page": nxt}
    req = StratumRequest(query="Трусы", count=2, is_seasonal=False)
    r = select_cards(req, fetch, get_card, 4.8, 100, 3, date(2026, 7, 1))
    assert r["found"] == 1          # больше одного магазина не набрать
    assert calls["n"] == 2          # была попытка второй страницы


def test_select_seasonal_slots():
    html = _html()
    cands = extract_candidates(html)
    order = [c.sku for c in prefilter(cands, [], 4.8, 100)]
    # назначим коллекции: чередуем сезонные и базовые, магазины уникальны
    colls = {}
    for i, sku in enumerate(order):
        colls[sku] = "Весна-лето 2026" if i % 2 == 0 else "Базовая коллекция"
    def get_card(url):
        import re
        sku = re.search(r"-(\d+)/?$", url).group(1)
        return _Card(sku, "seller_" + sku, colls.get(sku, "Демисезон"))
    # нужно 2 база + 2 сезон
    req = StratumRequest(query="Трусы", count=4, is_seasonal=True, base_share=0.5)
    r = select_cards(req, _page_once(html), get_card, 4.8, 100, 3, date(2026, 7, 1))
    # проверим, что набранное разложилось по слотам корректно
    kinds = [classify_collection(c.characteristics.get("Коллекция"))[0] for c in r["cards"]]
    assert kinds.count(Collection.BASE) <= 2
    assert kinds.count(Collection.SPRING_SUMMER) <= 2