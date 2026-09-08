# -*- coding: utf-8 -*-
"""
registry.py — реестр категорийных стратегий.

Единственное место, где категория из задачи сопоставляется с кодом.
Добавить категорию = импортировать её класс и добавить строку в _STRATEGIES.
"""
from app.categories.base import CategoryStrategy
from app.categories.clothing import ClothingStrategy
from app.categories.kids_clothing import KidsClothingStrategy
from app.core.exceptions import InvalidRequestError
from app.core.logging import get_logger

logger = get_logger(__name__)

_STRATEGIES: dict[str, CategoryStrategy] = {
    ClothingStrategy.name: ClothingStrategy(),
    KidsClothingStrategy.name: KidsClothingStrategy(),
}


def get_strategy(category: str | None) -> CategoryStrategy:
    """Стратегия по имени категории. Неизвестная категория — явная ошибка,
    а не молчаливое применение логики одежды."""
    key = (category or "clothing").strip()
    strategy = _STRATEGIES.get(key)
    if strategy is None:
        raise InvalidRequestError(
            f"Неизвестная категория {key!r}. Известные: {', '.join(sorted(_STRATEGIES))}. "
            f"Добавьте стратегию в app/categories/registry.py.",
            marketplace="ozon",
        )
    return strategy


def known_categories() -> list[str]:
    """Список поддерживаемых категорий (для диагностики и логов)."""
    return sorted(_STRATEGIES)