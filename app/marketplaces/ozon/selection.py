# -*- coding: utf-8 -*-
"""
selection.py — логика подбора карточек под страту: разобрать вход, определить сезон по дате,
посчитать, сколько и каких карточек нужно добрать с учётом исключений.
"""
import math
import pprint
from enum import Enum
from datetime import date
import re

from pydantic import BaseModel, Field

from app.core.exceptions import AntibotBlockedError
from app.core.logging import get_logger
from app.marketplaces.ozon.search_listing import extract_candidates, prefilter

logger = get_logger(__name__)


class Collection(str, Enum):
    """Тип коллекции карточки."""
    BASE = "base"                    # базовая (внесезонная) коллекция
    SPRING_SUMMER = "spring_summer"  # весна-лето
    AUTUMN_WINTER = "autumn_winter"  # осень-зима


class ExcludedCard(BaseModel):
    """Уже подобранная карточка (из базы), переданная в исключения."""
    sku: str = Field(..., description="Артикул")
    name: str | None = Field(None, description="Полное название (для проверки дублей)")
    url: str | None = Field(None, description="URL/slug (для проверки дублей)")
    seller: str | None = Field(None, description="Магазины должны отличаться в страте")
    collection: Collection | None = Field(
        None, description="Коллекция карточки: base / spring_summer / autumn_winter"
    )


class StratumRequest(BaseModel):
    """Одна страта на вход эндпоинта подбора."""
    query: str = Field(..., description="Поисковый запрос (что писать в поиск Ozon)")
    count: int = Field(..., ge=1, description="Сколько карточек нужно в сете (итоговое)")
    is_seasonal: bool = Field(..., description="Нужен ли сезонный сплит по коллекции")
    base_share: float | None = Field(
        None, ge=0, le=1,
        description="Доля базовой коллекции (обязательна при is_seasonal=true)",
    )
    exclude: list[ExcludedCard] = Field(
        default_factory=list, description="Уже подобранные карточки (исключить/не дублировать)"
    )


def current_season(today: date | None = None) -> Collection:
    """
    Текущий сезон по календарю:
      март–август (3–8)     -> весна-лето
      сентябрь–февраль (9–2) -> осень-зима
    today можно передать явно (для тестов); по умолчанию — сегодня.
    """
    today = today or date.today()
    m = today.month
    if 3 <= m <= 8:
        return Collection.SPRING_SUMMER
    return Collection.AUTUMN_WINTER


class Need(BaseModel):
    """Какие каточки требуется добрать для репрезентативного сета"""
    base: int = Field(0, description="Сколько базовых карточек добрать")
    seasonal: int = Field(0, description="Сколько сезонных (текущего сезона) добрать")
    season: Collection | None = Field(
        None, description="Какой сезон считается текущим (если сезонность есть)"
    )
    total: int = Field(0, description="Всего добрать (base + seasonal, или count если без сезона)")


def compute_need(req: StratumRequest, today: date | None = None) -> Need:
    """
    Посчитать потребность в карточках с учётом исключений.

    Без сезонности: нужно count, тип не важен; вычитаем валидные исключения.
    С сезонностью:  базовых = round(count*base_share), сезонных = count - базовых;
                    сезонные — только текущего сезона; вычитаем исключения по типам.
    Если исключений по типу больше, чем требуется, — это ошибка входных данных.
    """
    # --- без сезонности - коллекция не важна ---
    # count уже = потребность (дефицит): ядро вычло active при доборе.
    # exclude здесь НЕ вычитаем — он служит только фильтром дублей в prefilter.
    if not req.is_seasonal:
        return Need(base=0, seasonal=0, season=None, total=req.count)

    # --- с сезонностью: base_share обязателен ---
    if req.base_share is None:
        # это нарушение контракта; на уровне схемы можно сделать строже,
        # но подстрахуемся и здесь
        logger.warning("is_seasonal=true, но base_share не задан (query=%r)", req.query)
        raise ValueError("base_share обязателен при is_seasonal=true")

    season = current_season(today)
    need_base = int(math.floor(req.count * req.base_share + 0.5))
    need_seasonal = req.count - need_base

    # count уже = потребность (дефицит): ядро вычло active. exclude не вычитаем.
    left_base = need_base
    left_season = need_seasonal
    logger.info("Потребность рассчитана: base=%s, season=%s, сезон=%s",
                left_base, left_season, season.value)

    return Need(
        base=left_base,
        seasonal=left_season,
        season=season,
        total=left_base + left_season,
    )


def expected_season_year(today: date | None = None) -> int:
    """
    Минимальный допустимый год сезонной коллекции для текущего момента.
    Правило (карточки прошлого года — брак, текущий и будущий — ок → это нижняя граница):
      весна-лето (март–август)      -> текущий год
      осень-зима (сентябрь–декабрь) -> следующий год
      осень-зима (январь–февраль)   -> текущий год
    """
    today = today or date.today()
    m, y = today.month, today.year
    if 3 <= m <= 8:            # весна-лето
        return y
    if 9 <= m <= 12:           # осень-зима, конец года
        return y + 1
    return y                   # январь–февраль: осень-зима текущего года


# Ключевые слова коллекций (в поле "Коллекция" карточки)
_BASE_MARKERS = ("демисезон", "базовая")
_SS_MARKERS = ("весна-лето", "весна", "лето")
_AW_MARKERS = ("осень-зима", "осень", "зима")


def classify_collection(coll_text: str | None) -> tuple[Collection | None, int | None]:
    """
    Разобрать поле 'Коллекция' карточки в (тип, год).
      'Весна-лето 2026' -> (SPRING_SUMMER, 2026)
      'Базовая коллекция' -> (BASE, None)
      'Демисезон' -> (BASE, None)
      'Лето' -> (SPRING_SUMMER, None)   # год не указан
    Если распознать не удалось -> (None, None).
    """
    if not coll_text:
        return None, None
    t = coll_text.strip().lower()

    year = None
    ym = re.search(r"(20\d{2})", t)
    if ym:
        year = int(ym.group(1))

    # базовая/всесезонная — проверяем первой (демисезон не сезон)
    if any(mk in t for mk in _BASE_MARKERS):
        return Collection.BASE, year
    # осень-зима проверяем до "весна-лето", т.к. слова не пересекаются, порядок не критичен
    if any(mk in t for mk in _AW_MARKERS):
        return Collection.AUTUMN_WINTER, year
    if any(mk in t for mk in _SS_MARKERS):
        return Collection.SPRING_SUMMER, year
    return None, None


def card_matches_slot(card_collection_text: str | None,
                      need_kind: str,
                      today: date | None = None,
                      sku: str | None = None) -> bool:
    """
    Подходит ли карточка под требуемый «слот» коллекции.
      need_kind: 'base'  -> нужна всесезонная (Демисезон/Базовая)
                 'season'-> нужна коллекция текущего сезона с годом >= расчётного
    Пишет в INFO причину решения (важно для наблюдаемости подбора).
    """
    kind, year = classify_collection(card_collection_text)
    tag = f"sku={sku} " if sku else ""

    if need_kind == "base":
        ok = (kind == Collection.BASE)
        logger.info("%sколлекция=%r -> тип=%s | нужен BASE -> %s",
                    tag, card_collection_text, kind.value if kind else None,
                    "подходит" if ok else "не подходит")
        return ok

    # need_kind == "season"
    season = current_season(today)
    if kind != season:
        logger.info("%sколлекция=%r -> тип=%s | нужен сезон %s -> не подходит",
                    tag, card_collection_text, kind.value if kind else None, season.value)
        return False

    # сезон совпал; проверяем год, если он указан
    if year is None:
        logger.info("%sколлекция=%r -> сезон %s совпал, год не указан -> подходит",
                    tag, card_collection_text, season.value)
        return True

    min_year = expected_season_year(today)
    ok = year >= min_year
    logger.info("%sколлекция=%r -> сезон %s, год %s (нужен >= %s) -> %s",
                tag, card_collection_text, season.value, year, min_year,
                "подходит" if ok else "не подходит (прошлогодняя)")
    return ok


def seller_is_free(seller_id: str | None, taken_seller_ids: set[str],
                   sku: str | None = None) -> bool:
    """
    Магазин ещё не занят -- проверяю среди уже отобранных в этом запросе и среди карточек страты из БД
    Сравнение по seller.id. Карточка без seller.id считается непроходной
    (не можем гарантировать уникальность) — пишем WARNING.
    """
    tag = f"sku={sku} " if sku else ""
    if not seller_id:
        logger.warning("%sу карточки нет seller.id — пропускаю (уникальность магазина не гарантируется)", tag)
        return False
    if seller_id in taken_seller_ids:
        logger.info("%sмагазин %s уже занят в этом запросе -> не подходит", tag, seller_id)
        return False
    logger.info("%sмагазин %s свободен -> подходит", tag, seller_id)
    return True


def _reviews_to_int(value) -> int | None:
    """Безопасно привести reviews_count карточки (строка '103', None, мусор) к int."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _rating_to_float(value) -> float | None:
    """Безопасно привести rating карточки (строка '4.9', None) к float."""
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
    today: date | None = None,
) -> dict:
    """
    Подобрать карточки под страту. Возвращает
      {"cards": [OzonCard...], "requested": int, "found": int}.
    Ленивая пагинация: следующая страница берётся, только если не хватило.
    Весь процесс подробно логируется на INFO.
    """
    need = compute_need(req, today)   # шаг 1: сколько base / season нужно
    logger.info("=== ПОДБОР СТАРТ | query=%r | нужно всего=%s (base=%s, season=%s, сезон=%s) ===",
                req.query, need.total, need.base, need.seasonal,
                need.season.value if need.season else "нет")
    if need.total == 0:
        logger.info("Подбор не требуется: потребность 0 (всё покрыто исключениями)")
        return {"cards": [], "requested": req.count, "found": 0}

    taken_sellers: set[str] = set()
    for excluded in req.exclude:
        if excluded.seller:
            taken_sellers.add(excluded.seller)
    logger.debug("Занятые магазины (из исключений): %s", taken_sellers)

    picked: list = []                 # список OzonCard
    got_base = 0
    got_season = 0

    page_url = None                   # первая страница — по query
    for page_no in range(1, max_pages + 1):
        raw = fetch_page(page_url)     # {"html","url","next_page"}
        logger.info("Страница %s: %s", page_no, raw["url"])

        cands = extract_candidates(raw["html"])
        logger.info("Страница %s: плиток извлечено=%s", page_no, len(cands))

        pf = prefilter(cands, req.exclude, min_rating, min_reviews)
        logger.info("Страница %s: после первичного отсева=%s (рейтинг>=%s, отзывы>%s, дедуп, исключения)",
                    page_no, len(pf), min_rating, min_reviews)

        for c in pf:
            # уже набрали всё?
            if _slots_full(req, need, got_base, got_season):
                break

            logger.info("Захожу в карточку sku=%s (%s)", c.sku, c.url)
            try:
                card = get_card(c.url)  # переиспуемый путь card/by-url
            except AntibotBlockedError:
                raise  # блок пробрасываем как есть
            except Exception as e:
                logger.warning("sku=%s: карточку получить не удалось (%s) — пропуск", c.sku, e)
                continue

            # Финальная проверка порога по данным карточки. В выдаче
            # рейтинг/отзывы бывают суммарными по нескольким карточкам. Порог тот же: отзывы
            # строго больше min_reviews, рейтинг не ниже min_rating.
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

            if not seller_is_free(seller_id, taken_sellers, sku=c.sku):
                continue

            # определяем слот
            if not req.is_seasonal:
                # несезонный: один общий слот, коллекцию не проверяем
                picked.append(card); taken_sellers.add(seller_id); got_season += 0
                got_base += 1  # используем got_base как общий счётчик
                logger.info("ВЗЯТА sku=%s | магазин=%s | набрано %s/%s",
                            c.sku, seller_id, len(picked), need.total)
                continue

            coll = card.characteristics.get("Коллекция")
            # пробуем в тот слот, где ещё есть место: сначала season, потом base
            if got_season < need.seasonal and card_matches_slot(coll, "season", today, c.sku):
                picked.append(card); taken_sellers.add(seller_id); got_season += 1
                logger.info("ВЗЯТА sku=%s в СЕЗОННЫЙ слот | магазин=%s | season %s/%s, base %s/%s",
                            c.sku, seller_id, got_season, need.seasonal, got_base, need.base)
            elif got_base < need.base and card_matches_slot(coll, "base", today, c.sku):
                picked.append(card); taken_sellers.add(seller_id); got_base += 1
                logger.info("ВЗЯТА sku=%s в БАЗОВЫЙ слот | магазин=%s | season %s/%s, base %s/%s",
                            c.sku, seller_id, got_season, need.seasonal, got_base, need.base)
            else:
                logger.info("sku=%s не подошла ни в один открытый слот — пропуск", c.sku)

        if _slots_full(req, need, got_base, got_season):
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

        if _slots_full(req, need, got_base, got_season):
            logger.info("Набрано нужное количество — останавливаюсь на странице %s", page_no)
            break

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

        logger.info("Не хватает (набрано %s/%s) — беру страницу %s", len(picked), need.total, page_no + 1)
    else:
        logger.info("Достигнут потолок страниц (%s)", max_pages)

    found = len(picked)
    if found < need.total:
        logger.info("=== ПОДБОР ЗАВЕРШЁН | набрано %s из %s (меньше нужного — штатный исход) ===",
                    found, need.total)
    else:
        logger.info("=== ПОДБОР ЗАВЕРШЁН | набрано %s из %s ===", found, need.total)
    return {"cards": picked, "requested": req.count, "found": found}


def _slots_full(req: StratumRequest, need: "Need", got_base: int, got_season: int) -> bool:
    """Все требуемые слоты закрыты?"""
    if not req.is_seasonal:
        return got_base >= need.total     # got_base как общий счётчик
    return got_base >= need.base and got_season >= need.seasonal