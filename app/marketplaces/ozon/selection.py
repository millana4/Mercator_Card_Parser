# -*- coding: utf-8 -*-
"""
selection.py — оркестрация подбора карточек под страту.

Категорийная логика (сезоны, коллекции, пороги, уникальность продавца)
живёт в app/categories/. Здесь остаётся то, что одинаково для всех
категорий: листание выдачи, первичный отсев, заход в карточку, учёт слотов.

Модели контракта (StratumRequest, ExcludedCard, Collection, Need) переехали
в app/categories/models.py и реэкспортируются отсюда для совместимости.
"""
import re

from app.categories.base import CategoryStrategy
from app.categories.models import Collection, ExcludedCard, Need, StratumRequest
from app.categories.registry import get_strategy
from app.core.exceptions import AntibotBlockedError
from app.core.logging import get_logger
from app.marketplaces.ozon.search_listing import extract_candidates, prefilter

logger = get_logger(__name__)

# Реэкспорт: внешний код и тесты продолжают импортировать эти имена из selection.
__all__ = [
    "Collection", "ExcludedCard", "StratumRequest", "Need",
    "select_cards", "seller_is_free",
]


def seller_is_free(seller_id: str | None, taken_seller_ids: set[str],
                   sku: str | None = None) -> bool:
    """
    Магазин ещё не занят — проверяем среди отобранных в этом запросе и среди
    карточек страты из базы. Сравнение по seller.id. Карточка без seller.id
    считается непроходной (уникальность не гарантируется).
    """
    tag = f"sku={sku} " if sku else ""
    if not seller_id:
        logger.warning("%sу карточки нет seller.id — пропускаю "
                       "(уникальность магазина не гарантируется)", tag)
        return False
    if seller_id in taken_seller_ids:
        logger.info("%sмагазин %s уже занят в этом запросе -> не подходит", tag, seller_id)
        return False
    logger.info("%sмагазин %s свободен -> подходит", tag, seller_id)
    return True


def _reviews_to_int(value) -> int | None:
    """Безопасно привести reviews_count карточки ('103', None, мусор) к int."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _rating_to_float(value) -> float | None:
    """Безопасно привести rating карточки ('4.9', None) к float."""
    if value is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(value))
    return float(m.group(0).replace(",", ".")) if m else None


def select_cards(
    req: StratumRequest,
    fetch_page,          # callable: (url|None) -> {"html","url","next_page"}
    get_card,            # callable: (url) -> OzonCard  (это service.get_card)
    min_rating: float,
    min_reviews: int,
    max_pages: int,
    today=None,
    strategy: CategoryStrategy | None = None,
) -> dict:
    """
    Подобрать карточки под страту. Возвращает
      {"cards": [OzonCard...], "requested": int, "found": int}.

    Стратегия категории берётся из req.category, если не передана явно.
    Пороги min_rating/min_reviews приходят уже согласованными с категорией
    (см. service.select_for_stratum).

    Ленивая пагинация: следующая страница берётся, только если не хватило.
    """
    strategy = strategy or get_strategy(req.category)

    need = strategy.compute_need(req, today)
    logger.info("=== ПОДБОР СТАРТ | категория=%s | query=%r | нужно всего=%s "
                "(base=%s, season=%s, сезон=%s) | пороги: рейтинг>=%s, отзывы>%s ===",
                strategy.name, req.query, need.total, need.base, need.seasonal,
                need.season.value if need.season else "нет", min_rating, min_reviews)

    if need.total == 0:
        logger.info("Подбор не требуется: потребность 0")
        return {"cards": [], "requested": req.count, "found": 0}

    taken_sellers: set[str] = set()
    if strategy.require_unique_seller:
        for excluded in req.exclude:
            if excluded.seller:
                taken_sellers.add(excluded.seller)
        logger.debug("Занятые магазины (из исключений): %s", taken_sellers)
    else:
        logger.info("Категория %s: уникальность продавца не требуется", strategy.name)

    picked: list = []
    got_base = 0
    got_season = 0
    page_url = None                   # первая страница — по query

    for page_no in range(1, max_pages + 1):
        raw = fetch_page(page_url)
        logger.info("Страница %s: %s", page_no, raw["url"])

        cands = extract_candidates(raw["html"])
        logger.info("Страница %s: плиток извлечено=%s", page_no, len(cands))

        pf = prefilter(cands, req.exclude, min_rating, min_reviews)
        logger.info("Страница %s: после первичного отсева=%s "
                    "(рейтинг>=%s, отзывы>%s, дедуп, исключения)",
                    page_no, len(pf), min_rating, min_reviews)

        for c in pf:
            if strategy.slots_full(need, got_base, got_season):
                break

            logger.info("Захожу в карточку sku=%s (%s)", c.sku, c.url)
            try:
                card = get_card(c.url)
            except AntibotBlockedError:
                raise
            except Exception as e:
                logger.warning("sku=%s: карточку получить не удалось (%s) — пропуск", c.sku, e)
                continue

            # Финальная проверка порогов по данным карточки: в выдаче рейтинг
            # и отзывы бывают суммарными по нескольким карточкам.
            card_reviews = _reviews_to_int(card.reviews_count)
            card_rating = _rating_to_float(card.rating)
            if card_reviews is None or card_reviews <= min_reviews:
                logger.info("sku=%s: отзывов %s (нужно > %s) — пропуск по карточке",
                            c.sku, card_reviews, min_reviews)
                continue
            if card_rating is None or card_rating < min_rating:
                logger.info("sku=%s: рейтинг %s (нужно >= %s) — пропуск по карточке",
                            c.sku, card_rating, min_rating)
                continue

            seller_id = card.seller.id if card.seller else None
            if strategy.require_unique_seller and not seller_is_free(
                    seller_id, taken_sellers, sku=c.sku):
                continue

            slot = strategy.pick_slot(card, need, got_base, got_season, today, c.sku)
            if slot is None:
                logger.info("sku=%s не подошла ни в один открытый слот — пропуск", c.sku)
                continue

            picked.append(card)
            if seller_id:
                taken_sellers.add(seller_id)

            if slot == "season":
                got_season += 1
            else:
                got_base += 1

            logger.info("ВЗЯТА sku=%s в слот %s | магазин=%s | season %s/%s, base %s/%s",
                        c.sku, slot, seller_id, got_season, need.seasonal,
                        got_base, need.base if need.season else need.total)

        if strategy.slots_full(need, got_base, got_season):
            logger.info("Набрано нужное количество — останавливаюсь на странице %s", page_no)
            break

        # решаем, есть ли следующая страница
        current_page = raw.get("current_page")
        total_pages = raw.get("total_pages")
        nxt = raw.get("next_page")
        has_next_by_nuxt = (current_page is not None and total_pages is not None
                            and current_page < total_pages)

        logger.info("Страница %s: плиток=%s | currentPage=%s totalPages=%s | "
                    "nextPage=%s, есть_следующая(nuxt)=%s",
                    page_no, len(cands), current_page, total_pages,
                    "есть" if nxt else "нет", has_next_by_nuxt)

        # гибрид: nextPage приоритетно (готовый URL с токенами), иначе строим page=N
        if nxt:
            page_url = nxt
        elif has_next_by_nuxt:
            from app.marketplaces.ozon.search_listing import build_next_page_url
            page_url = build_next_page_url(raw["url"], current_page)
            logger.info("nextPage нет — строю следующую страницу по page: %s", page_url)
        else:
            logger.info("Следующей страницы нет (currentPage>=totalPages) на странице %s", page_no)
            break

        logger.info("Не хватает (набрано %s/%s) — беру страницу %s",
                    len(picked), need.total, page_no + 1)
    else:
        logger.info("Достигнут потолок страниц (%s)", max_pages)

    found = len(picked)
    logger.info("=== ПОДБОР ЗАВЕРШЁН | категория=%s | набрано %s из %s%s ===",
                strategy.name, found, need.total,
                " (меньше нужного — штатный исход)" if found < need.total else "")

    return {"cards": picked, "requested": req.count, "found": found}