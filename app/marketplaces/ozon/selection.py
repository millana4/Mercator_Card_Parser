# -*- coding: utf-8 -*-
"""
selection.py — оркестрация подбора карточек под страту.

Категорийная логика (сезоны, коллекции, пороги, уникальность продавца)
живёт в app/categories/. Здесь остаётся то, что одинаково для всех
категорий: листание выдачи, первичный отсев, заход в карточку, учёт слотов.

Выдача Ozon одна и догружается прокруткой (страниц нет). Подбор берёт кандидатов
догрузками: load_candidates(known, want) прокручивает выдачу и отдаёт кандидатов
с новыми артикулами (не из known); не хватило — следующая догрузка, до max_loads
раз или пока выдача не кончится. Один товар дважды не проверяется.

Приоритет частых SKU: ядро передаёт priority_skus — SKU, часто встречавшиеся
в поисковой выдаче страты (по убыванию частоты). В каждой догрузке после
первичного отсева такие кандидаты проверяются первыми; остальные — в прежнем
порядке. Число заходов в карточки не меняется.

Капча или сбой загрузки (AntibotBlockedError) после того, как что-то уже набрано:
подбор останавливается и отдаёт набранное до капчи с пометкой blocked; если не
набрано ничего — ошибка пробрасывается.

Модели контракта (StratumRequest, ExcludedCard, PrioritySku, Collection, Need)
переехали в app/categories/models.py и реэкспортируются отсюда для совместимости.
"""
import re

from app.categories.base import CategoryStrategy
from app.categories.models import Collection, ExcludedCard, Need, PrioritySku, StratumRequest
from app.categories.registry import get_strategy
from app.core.exceptions import AntibotBlockedError
from app.core.logging import get_logger
from app.marketplaces.ozon.search_listing import prefilter

logger = get_logger(__name__)

# Реэкспорт: внешний код и тесты продолжают импортировать эти имена из selection.
__all__ = [
    "Collection", "ExcludedCard", "PrioritySku", "StratumRequest", "Need",
    "select_cards", "seller_is_free", "prioritize",
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


def prioritize(cands: list, priority: list[PrioritySku]) -> list:
    """
    Кандидаты из списка частых SKU — первыми, в порядке списка (по убыванию
    частоты); остальные — в прежнем порядке (сортировка устойчивая).
    Пустой список — порядок не меняется.
    """
    if not priority:
        return cands
    rank: dict[str, int] = {}
    for i, p in enumerate(priority):
        rank.setdefault(str(p.sku), i)
    tail = len(rank)
    return sorted(cands, key=lambda c: rank.get(str(c.sku), tail))


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
    load_candidates,     # callable: (known: set[str], want: int) -> [Candidate] с новыми sku; [] - выдача кончилась
    get_card,            # callable: (url) -> OzonCard
    min_rating: float,
    min_reviews: int,
    max_loads: int,
    today=None,
    strategy: CategoryStrategy | None = None,
    pool_multiplier: int = 3,
    min_pool: int = 32,
) -> dict:
    """
    Подобрать карточки под страту. Возвращает
      {"cards": [OzonCard...], "requested": int, "found": int,
       "blocked": AntibotBlockedError | None}.
    blocked - подбор прерван капчей или сбоем загрузки, отдано набранное до неё.

    Стратегия категории берётся из req.category, если не передана явно.
    Пороги min_rating/min_reviews приходят уже согласованными с категорией
    (см. service.select_for_stratum).

    Догрузки: want = max(min_pool, недобор × pool_multiplier) новых плиток;
    следующая догрузка — только если не хватило. Частые SKU выдачи
    (req.priority_skus) в каждой догрузке проверяются первыми.
    """
    strategy = strategy or get_strategy(req.category)

    # Частота SKU в выдаче — для порядка кандидатов и лога.
    freq: dict[str, int] = {}
    for p in req.priority_skus:
        freq.setdefault(str(p.sku), p.count)

    need = strategy.compute_need(req, today)
    logger.info("=== ПОДБОР СТАРТ | категория=%s | query=%r | нужно всего=%s "
                "(base=%s, season=%s, сезон=%s) | пороги: рейтинг>=%s, отзывы>%s | "
                "частых в выдаче=%s ===",
                strategy.name, req.query, need.total, need.base, need.seasonal,
                need.season.value if need.season else "нет", min_rating, min_reviews,
                len(freq))

    if need.total == 0:
        logger.info("Подбор не требуется: потребность 0")
        return {"cards": [], "requested": req.count, "found": 0, "blocked": None}

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
    got_priority = 0
    seen: set[str] = set()           # артикулы, уже взятые из выдачи (дважды не проверяем)
    blocked: AntibotBlockedError | None = None

    def stop_on_block(exc: AntibotBlockedError) -> None:
        """Капча или сбой загрузки: ничего не набрано - ошибка; иначе - отдать набранное."""
        nonlocal blocked
        if not picked:
            raise exc
        blocked = exc
        logger.warning("Подбор прерван (%s) — отдаю набранное до капчи: %s из %s",
                       exc, len(picked), need.total)

    for load_no in range(1, max_loads + 1):
        want = max(min_pool, (need.total - len(picked)) * pool_multiplier)
        try:
            cands = load_candidates(seen, want)
        except AntibotBlockedError as exc:
            stop_on_block(exc)
            break
        cands = [c for c in cands if c.sku not in seen]
        if not cands:
            logger.info("Догрузка %s: новых плиток нет — выдача кончилась", load_no)
            break
        seen.update(c.sku for c in cands)
        logger.info("Догрузка %s: новых плиток=%s (просили %s)", load_no, len(cands), want)

        pf = prefilter(cands, req.exclude, min_rating, min_reviews)
        logger.info("Догрузка %s: после первичного отсева=%s "
                    "(рейтинг>=%s, отзывы>%s, дедуп, исключения)",
                    load_no, len(pf), min_rating, min_reviews)

        if freq:
            pf = prioritize(pf, req.priority_skus)
            on_page = sum(1 for c in pf if str(c.sku) in freq)
            logger.info("Догрузка %s: частых в выдаче среди кандидатов=%s — проверяю их первыми",
                        load_no, on_page)

        for c in pf:
            if strategy.slots_full(need, got_base, got_season):
                break

            logger.info("Захожу в карточку sku=%s (%s)", c.sku, c.url)
            try:
                card = get_card(c.url)
            except AntibotBlockedError as exc:
                stop_on_block(exc)
                break
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

            frequent = freq.get(str(c.sku))
            if frequent is not None:
                got_priority += 1

            logger.info("ВЗЯТА sku=%s в слот %s | магазин=%s | частая в выдаче: %s | "
                        "season %s/%s, base %s/%s",
                        c.sku, slot, seller_id,
                        frequent if frequent is not None else "нет",
                        got_season, need.seasonal,
                        got_base, need.base if need.season else need.total)

        if blocked is not None:
            break
        if strategy.slots_full(need, got_base, got_season):
            logger.info("Набрано нужное количество — останавливаюсь на догрузке %s", load_no)
            break
        logger.info("Не хватает (набрано %s/%s) — догружаю выдачу", len(picked), need.total)
    else:
        logger.info("Достигнут предел догрузок (%s)", max_loads)

    found = len(picked)
    logger.info("=== ПОДБОР ЗАВЕРШЁН | категория=%s | набрано %s из %s%s | "
                "из них частых в выдаче %s ===",
                strategy.name, found, need.total,
                " (прерван капчей)" if blocked is not None
                else (" (меньше нужного — штатный исход)" if found < need.total else ""),
                got_priority)

    return {"cards": picked, "requested": req.count, "found": found, "blocked": blocked}