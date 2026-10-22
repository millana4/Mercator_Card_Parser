# -*- coding: utf-8 -*-
"""
models.py — модели контракта подбора, общие для всех категорий.

Вынесены из selection.py, чтобы стратегии категорий могли их использовать
без кольцевого импорта (selection импортирует стратегии, стратегии — модели).
"""
from enum import Enum

from pydantic import BaseModel, Field


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


class PrioritySku(BaseModel):
    """SKU, часто встречавшийся в поисковой выдаче страты (список от ядра)."""
    sku: str = Field(..., description="Артикул")
    count: int = Field(0, description="В скольких прогонах выдачи встретился за окно")


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
    category: str = Field(
        "clothing",
        description="Категория товаров: определяет логику подбора "
                    "(clothing, kids_clothing, ...)",
    )
    priority_skus: list[PrioritySku] = Field(
        default_factory=list,
        description="Частые SKU поисковой выдачи страты по убыванию частоты: "
                    "среди прошедших первичный отсев на странице проверяются первыми",
    )


class Need(BaseModel):
    """Какие карточки требуется добрать для репрезентативного сета."""
    base: int = Field(0, description="Сколько базовых карточек добрать")
    seasonal: int = Field(0, description="Сколько сезонных (текущего сезона) добрать")
    season: Collection | None = Field(
        None, description="Какой сезон считается текущим (если сезонность есть)"
    )
    total: int = Field(0, description="Всего добрать (base + seasonal, или count если без сезона)")