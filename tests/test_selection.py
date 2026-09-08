# -*- coding: utf-8 -*-
"""
test_selection.py — тесты подбора карточек под страту.

Работают БЕЗ сети: выдача берётся из сохранённого HTML (фикстура),
заход в карточку и открытие страниц мокаются. Проверяют извлечение
кандидатов, первичный отсев, расчёт потребности, сезонный фильтр,
уникальность магазина и оркестратор набора с пагинацией.

Категорийная логика (сезоны, коллекции, пороги, уникальность продавца)
живёт в стратегиях app/categories — они и тестируются напрямую.

Запуск: pytest tests/test_selection.py -v
"""
import os
from datetime import date

from app.categories.clothing import ClothingStrategy
from app.categories.kids_clothing import KidsClothingStrategy
from app.categories.models import Collection, ExcludedCard, StratumRequest
from app.categories.registry import get_strategy, known_categories
from app.marketplaces.ozon.search_listing import (
    extract_candidates, extract_next_page, prefilter, dedupe, passes_quality,
)
from app.marketplaces.ozon.selection import seller_is_free, select_cards

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "ozon_search_trusy_zhenskie.html")

# Стратегии под рукой: большинство проверок — про обычную одежду.
CLOTHING = ClothingStrategy()
KIDS = KidsClothingStrategy()


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
#  Реестр категорий                                                    #
# =================================================================== #
def test_registry_returns_strategies():
    assert get_strategy("clothing").name == "clothing"
    assert get_strategy("kids_clothing").name == "kids_clothing"
    # по умолчанию — одежда (обратная совместимость со старым контрактом)
    assert get_strategy(None).name == "clothing"


def test_registry_rejects_unknown_category():
    import pytest
    from app.core.exceptions import InvalidRequestError
    with pytest.raises(InvalidRequestError):
        get_strategy("автозапчасти")


def test_known_categories_listed():
    cats = known_categories()
    assert "clothing" in cats and "kids_clothing" in cats


# =================================================================== #
#  Сезон / год / коллекция (стратегия одежды)                          #
# =================================================================== #
def test_current_season():
    assert CLOTHING.current_season(date(2026, 7, 1)) == Collection.SPRING_SUMMER
    assert CLOTHING.current_season(date(2026, 1, 1)) == Collection.AUTUMN_WINTER
    assert CLOTHING.current_season(date(2026, 3, 1)) == Collection.SPRING_SUMMER
    assert CLOTHING.current_season(date(2026, 9, 1)) == Collection.AUTUMN_WINTER


def test_expected_year():
    assert CLOTHING.expected_season_year(date(2026, 7, 1)) == 2026   # ВЛ
    assert CLOTHING.expected_season_year(date(2026, 10, 1)) == 2027  # ОЗ конец года
    assert CLOTHING.expected_season_year(date(2026, 1, 1)) == 2026   # ОЗ янв


def test_classify_collection():
    cc = CLOTHING.classify_collection
    assert cc("Весна-лето 2026") == (Collection.SPRING_SUMMER, 2026)
    assert cc("Базовая коллекция") == (Collection.BASE, None)
    assert cc("Демисезон") == (Collection.BASE, None)
    assert cc("Лето")[0] == Collection.SPRING_SUMMER
    assert cc("Осень-зима 2027") == (Collection.AUTUMN_WINTER, 2027)


def test_card_matches_slot_season():
    jul = date(2026, 7, 1)
    m = CLOTHING.card_matches_slot
    assert m("Весна-лето 2026", "season", jul) is True
    assert m("Весна-лето 2025", "season", jul) is False  # прошлогодняя
    assert m("Лето", "season", jul) is True              # без года
    assert m("Базовая коллекция", "season", jul) is False


def test_card_matches_slot_base():
    jul = date(2026, 7, 1)
    m = CLOTHING.card_matches_slot
    assert m("Базовая коллекция", "base", jul) is True
    assert m("Демисезон", "base", jul) is True
    assert m("Весна-лето 2026", "base", jul) is False


def test_seller_uniqueness():
    taken = set()
    assert seller_is_free("111", taken) is True
    taken.add("111")
    assert seller_is_free("111", taken) is False   # занят
    assert seller_is_free("222", taken) is True
    assert seller_is_free(None, taken) is False     # без id — не проходит


# =================================================================== #
#  Детская одежда: школьная коллекция и продавцы                       #
# =================================================================== #
def test_kids_school_collection_is_base():
    """«Школа 2026» у детской одежды — базовая коллекция.
    У взрослой она не распознаётся вовсе."""
    assert KIDS.classify_collection("Школа 2026") == (Collection.BASE, 2026)
    assert KIDS.classify_collection("Школьная коллекция")[0] == Collection.BASE
    # у взрослой одежды такого правила нет: без типа год не возвращается
    assert CLOTHING.classify_collection("Школа 2026") == (None, None)


def test_kids_school_fits_base_slot():
    jul = date(2026, 7, 1)
    assert KIDS.card_matches_slot("Школа 2026", "base", jul) is True
    assert KIDS.card_matches_slot("Школа 2026", "season", jul) is False


def test_kids_keeps_clothing_seasons():
    """Остальная сезонная логика у детской наследуется без изменений."""
    jul = date(2026, 7, 1)
    assert KIDS.classify_collection("Весна-лето 2026") == (Collection.SPRING_SUMMER, 2026)
    assert KIDS.card_matches_slot("Весна-лето 2025", "season", jul) is False


def test_kids_does_not_require_unique_seller():
    assert CLOTHING.require_unique_seller is True
    assert KIDS.require_unique_seller is False


# =================================================================== #
#  Пороги качества                                                     #
# =================================================================== #
class _Policy:
    """Заглушка политики маркетплейса: только пороги."""
    min_rating = 4.8
    min_reviews = 30


def test_thresholds_fall_back_to_policy():
    """Категория без своих порогов берёт их из политики маркетплейса."""
    assert CLOTHING.thresholds(_Policy()) == (4.8, 30)
    assert KIDS.thresholds(_Policy()) == (4.8, 30)


def test_thresholds_category_overrides_policy():
    """Категория со своими порогами перекрывает политику."""
    class _Loose(ClothingStrategy):
        name = "loose"
        min_rating = 4.5
        min_reviews = 10

    assert _Loose().thresholds(_Policy()) == (4.5, 10)


# =================================================================== #
#  Расчёт потребности                                                 #
# =================================================================== #
def test_need_non_seasonal():
    req = StratumRequest(query="x", count=4, is_seasonal=False,
                         exclude=[ExcludedCard(sku="a")])
    need = CLOTHING.compute_need(req)
    assert need.total == 4


def test_need_seasonal_split():
    # count=4, доля 0.5 -> 2 база + 2 сезон
    req = StratumRequest(query="x", count=4, is_seasonal=True, base_share=0.5)
    need = CLOTHING.compute_need(req, date(2026, 7, 1))
    assert (need.base, need.seasonal) == (2, 2)


def test_need_seasonal_round_half_up():
    # count=5, доля 0.5 -> 3 база (в пользу базы) + 2 сезон
    req = StratumRequest(query="x", count=5, is_seasonal=True, base_share=0.5)
    need = CLOTHING.compute_need(req, date(2026, 7, 1))
    assert (need.base, need.seasonal) == (3, 2)


def test_need_excludes_by_type():
    # exclude не уменьшает потребность по типам: count уже равен дефициту.
    req = StratumRequest(query="x", count=4, is_seasonal=True, base_share=0.5,
                         exclude=[ExcludedCard(sku="1", collection=Collection.BASE),
                                  ExcludedCard(sku="2", collection=Collection.BASE),
                                  ExcludedCard(sku="3", collection=Collection.SPRING_SUMMER)])
    need = CLOTHING.compute_need(req, date(2026, 7, 1))
    assert (need.base, need.seasonal) == (2, 2)


def test_need_seasonal_wrong_season_excludes_ignored():
    req = StratumRequest(query="x", count=4, is_seasonal=True, base_share=0.5,
                         exclude=[ExcludedCard(sku="1", collection=Collection.BASE),
                                  ExcludedCard(sku="2", collection=Collection.BASE),
                                  ExcludedCard(sku="3", collection=Collection.SPRING_SUMMER)])
    need = CLOTHING.compute_need(req, date(2026, 1, 1))
    assert (need.base, need.seasonal) == (2, 2)


def test_need_kids_same_as_clothing():
    """Детская одежда считает потребность так же, как взрослая."""
    req = StratumRequest(query="x", count=5, is_seasonal=True, base_share=0.5,
                         category="kids_clothing")
    need = KIDS.compute_need(req, date(2026, 7, 1))
    assert (need.base, need.seasonal) == (3, 2)


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


def test_select_kids_ignores_seller_dedup():
    """У детской одежды один продавец не мешает: набираем сколько нужно."""
    html = _html()

    def get_card(url):
        import re
        sku = re.search(r"-(\d+)/?$", url).group(1)
        return _Card(sku, "ONE_SHOP")   # все карточки одного магазина

    req = StratumRequest(query="Футболка для девочки", count=2,
                         is_seasonal=False, category="kids_clothing")
    r = select_cards(req, _page_once(html), get_card, 4.8, 100, 3, date(2026, 7, 1))
    assert r["found"] == 2   # уникальность продавца не требуется


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

    kinds = [CLOTHING.classify_collection(c.characteristics.get("Коллекция"))[0]
             for c in r["cards"]]
    assert kinds.count(Collection.BASE) <= 2
    assert kinds.count(Collection.SPRING_SUMMER) <= 2


def test_select_kids_takes_school_collection():
    """Школьные карточки попадают в базовый слот у детской одежды."""
    html = _html()

    def get_card(url):
        import re
        sku = re.search(r"-(\d+)/?$", url).group(1)
        return _Card(sku, "seller_" + sku, "Школа 2026")

    req = StratumRequest(query="Рубашка для мальчика", count=2, is_seasonal=True,
                         base_share=1.0, category="kids_clothing")
    r = select_cards(req, _page_once(html), get_card, 4.8, 100, 3, date(2026, 7, 1))
    assert r["found"] == 2