# -*- coding: utf-8 -*-
"""
base.py — интерфейс категорийной стратегии подбора.

Стратегия отвечает на вопросы, которые у разных категорий решаются по-разному:
сколько и каких карточек нужно, подходит ли конкретная карточка в открытый
слот, какие пороги качества применять, обязателен ли уникальный продавец.

Всё, что не зависит от категории (разбор выдачи, дедуп, пагинация, заход
в карточку), остаётся в selection.py и search_listing.py.

Новая категория = новый класс-наследник + строка в registry.py.
Существующие категории при этом не трогаются.
"""
from abc import ABC, abstractmethod
from datetime import date

from app.categories.models import Need, StratumRequest


class CategoryStrategy(ABC):
    """Базовая стратегия подбора для одной категории товаров."""

    # Имя категории — совпадает с тем, что приходит в задаче от ядра.
    name: str = "base"

    # Пороги качества кандидата. None означает «взять из политики маркетплейса».
    min_rating: float | None = None
    min_reviews: int | None = None

    # Нужен ли уникальный продавец в пределах страты.
    require_unique_seller: bool = True

    # ------------------------------------------------------------------ #
    def thresholds(self, policy) -> tuple[float, int]:
        """Пороги рейтинга и отзывов: категория переопределяет политику
        маркетплейса, если задала свои значения."""
        rating = self.min_rating if self.min_rating is not None else policy.min_rating
        reviews = self.min_reviews if self.min_reviews is not None else policy.min_reviews
        return rating, reviews

    @abstractmethod
    def compute_need(self, req: StratumRequest, today: date | None = None) -> Need:
        """Сколько карточек и в какие слоты нужно добрать."""

    @abstractmethod
    def pick_slot(self, card, need: Need, got_base: int, got_season: int,
                  today: date | None = None, sku: str | None = None) -> str | None:
        """В какой открытый слот годится карточка.

        Возвращает 'base', 'season' или None, если карточка не подходит
        ни в один слот, где ещё есть место.
        """

    @abstractmethod
    def slots_full(self, need: Need, got_base: int, got_season: int) -> bool:
        """Все требуемые слоты закрыты?"""