# -*- coding: utf-8 -*-
"""
clothing.py — стратегия подбора для категории «одежда».

Специфика категории: сезонный сплит. Часть страты набирается базовой
(всесезонной) коллекцией, часть — коллекцией текущего сезона, причём
прошлогодние сезонные карточки не годятся.
"""
import math
import re
from datetime import date

from app.categories.base import CategoryStrategy
from app.categories.models import Collection, Need, StratumRequest
from app.core.logging import get_logger

logger = get_logger(__name__)


class ClothingStrategy(CategoryStrategy):
    """Одежда: базовая коллекция + коллекция текущего сезона."""

    name = "clothing"
    require_unique_seller = True

    # Ключевые слова коллекций (поле «Коллекция» в характеристиках карточки).
    # Наследники могут расширить набор, не трогая логику классификации.
    BASE_MARKERS: tuple[str, ...] = ("демисезон", "базовая")
    SPRING_SUMMER_MARKERS: tuple[str, ...] = ("весна-лето", "весна", "лето")
    AUTUMN_WINTER_MARKERS: tuple[str, ...] = ("осень-зима", "осень", "зима")

    # ------------------------------------------------------------------ #
    #  Календарь сезонов                                                  #
    # ------------------------------------------------------------------ #
    def current_season(self, today: date | None = None) -> Collection:
        """
        Текущий сезон по календарю:
          март–август (3–8)      -> весна-лето
          сентябрь–февраль (9–2) -> осень-зима
        """
        today = today or date.today()
        m = today.month
        if 3 <= m <= 8:
            return Collection.SPRING_SUMMER
        return Collection.AUTUMN_WINTER

    def expected_season_year(self, today: date | None = None) -> int:
        """
        Минимальный допустимый год сезонной коллекции.
          весна-лето (март–август)      -> текущий год
          осень-зима (сентябрь–декабрь) -> следующий год
          осень-зима (январь–февраль)   -> текущий год
        """
        today = today or date.today()
        m, y = today.month, today.year
        if 3 <= m <= 8:
            return y
        if 9 <= m <= 12:
            return y + 1
        return y

    # ------------------------------------------------------------------ #
    #  Классификация коллекции                                            #
    # ------------------------------------------------------------------ #
    def classify_collection(self, coll_text: str | None) -> tuple[Collection | None, int | None]:
        """
        Разобрать поле «Коллекция» карточки в (тип, год).
          'Весна-лето 2026'   -> (SPRING_SUMMER, 2026)
          'Базовая коллекция' -> (BASE, None)
          'Демисезон'         -> (BASE, None)
          'Лето'              -> (SPRING_SUMMER, None)
        Не удалось распознать -> (None, None).
        """
        if not coll_text:
            return None, None

        t = coll_text.strip().lower()

        year = None
        ym = re.search(r"(20\d{2})", t)
        if ym:
            year = int(ym.group(1))

        # базовая/всесезонная — первой (демисезон не сезон)
        if any(mk in t for mk in self.BASE_MARKERS):
            return Collection.BASE, year
        if any(mk in t for mk in self.AUTUMN_WINTER_MARKERS):
            return Collection.AUTUMN_WINTER, year
        if any(mk in t for mk in self.SPRING_SUMMER_MARKERS):
            return Collection.SPRING_SUMMER, year

        return None, None

    def card_matches_slot(self, coll_text: str | None, need_kind: str,
                          today: date | None = None, sku: str | None = None) -> bool:
        """
        Подходит ли карточка под требуемый слот.
          need_kind: 'base'   -> нужна всесезонная (Демисезон/Базовая)
                     'season' -> коллекция текущего сезона с годом >= расчётного
        """
        kind, year = self.classify_collection(coll_text)
        tag = f"sku={sku} " if sku else ""

        if need_kind == "base":
            ok = (kind == Collection.BASE)
            logger.info("%sколлекция=%r -> тип=%s | нужен BASE -> %s",
                        tag, coll_text, kind.value if kind else None,
                        "подходит" if ok else "не подходит")
            return ok

        season = self.current_season(today)
        if kind != season:
            logger.info("%sколлекция=%r -> тип=%s | нужен сезон %s -> не подходит",
                        tag, coll_text, kind.value if kind else None, season.value)
            return False

        if year is None:
            logger.info("%sколлекция=%r -> сезон %s совпал, год не указан -> подходит",
                        tag, coll_text, season.value)
            return True

        min_year = self.expected_season_year(today)
        ok = year >= min_year
        logger.info("%sколлекция=%r -> сезон %s, год %s (нужен >= %s) -> %s",
                    tag, coll_text, season.value, year, min_year,
                    "подходит" if ok else "не подходит (прошлогодняя)")
        return ok

    # ------------------------------------------------------------------ #
    #  Реализация интерфейса                                              #
    # ------------------------------------------------------------------ #
    def compute_need(self, req: StratumRequest, today: date | None = None) -> Need:
        """
        Без сезонности: нужно count, коллекция не важна.
        С сезонностью: базовых = round(count * base_share), сезонных — остаток.

        count уже равен дефициту: ядро вычло active при доборе, поэтому
        exclude здесь не вычитается (он служит фильтром дублей в prefilter).
        """
        if not req.is_seasonal:
            return Need(base=0, seasonal=0, season=None, total=req.count)

        if req.base_share is None:
            logger.warning("is_seasonal=true, но base_share не задан (query=%r)", req.query)
            raise ValueError("base_share обязателен при is_seasonal=true")

        season = self.current_season(today)
        need_base = int(math.floor(req.count * req.base_share + 0.5))
        need_seasonal = req.count - need_base

        logger.info("Потребность рассчитана: base=%s, season=%s, сезон=%s",
                    need_base, need_seasonal, season.value)

        return Need(
            base=need_base,
            seasonal=need_seasonal,
            season=season,
            total=need_base + need_seasonal,
        )

    def pick_slot(self, card, need: Need, got_base: int, got_season: int,
                  today: date | None = None, sku: str | None = None) -> str | None:
        """Сначала пробуем сезонный слот, затем базовый — если там есть место."""
        # Несезонная страта: один общий слот, коллекция не проверяется.
        if need.season is None:
            return "base" if got_base < need.total else None

        coll = card.characteristics.get("Коллекция")

        if got_season < need.seasonal and self.card_matches_slot(coll, "season", today, sku):
            return "season"
        if got_base < need.base and self.card_matches_slot(coll, "base", today, sku):
            return "base"
        return None

    def slots_full(self, need: Need, got_base: int, got_season: int) -> bool:
        if need.season is None:
            return got_base >= need.total      # got_base как общий счётчик
        return got_base >= need.base and got_season >= need.seasonal