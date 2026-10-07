# -*- coding: utf-8 -*-
"""
ozon_models.py — структурированная модель карточки товара Ozon.

Это «чистое» представление товара: только нужные для анализа поля, собранные
из сырых данных страницы. Заполняется трансформером ozon_transform.py.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class OzonSeller(BaseModel):
    """Данные продавца."""
    id: str | None = Field(None, description="ID продавца (sellerId)")
    name: str | None = Field(None, description="Название магазина на Ozon")
    legal_name: str | None = Field(None, description="Юр. название (ООО/АО/ИП ...)")
    ogrn: str | None = Field(None, description="ОГРН/ОГРНИП продавца")


class OzonLocation(BaseModel):
    """
    Регион, в контексте которого Ozon отдал страницу (цены/наличие).
    Берётся из location.current сырого состояния. Определяется по IP окружения.
    """
    city: str | None = Field(None, description="Город (name)")
    country: str | None = Field(None, description="Страна")
    country_code: str | None = Field(None, description="Код страны (например, RUS)")
    area_id: int | None = Field(None, description="ID региона Ozon (areaId)")
    fias: str | None = Field(None, description="ФИАС-идентификатор региона")
    timezone: str | None = Field(None, description="Часовой пояс (например, UTC+3)")


class OzonPrice(BaseModel):
    """Цены товара в рублях, числом (без символа валюты и разделителей)."""
    card_price: float | None = Field(None, description="Цена с Ozon Картой (со скидкой)")
    price: float | None = Field(None, description="Текущая цена")
    original_price: float | None = Field(None, description="Цена без скидки (зачёркнутая)")


class OzonVariant(BaseModel):
    """Вариант товара (обычно размер) — отдельный sku со своей доступностью."""
    sku: str | None = Field(None, description="Артикул этого варианта")
    value: str | None = Field(None, description="Значение варианта (например, '48 RU / L')")
    availability: str | None = Field(None, description="Доступность (inStock/outOfStock)")
    price: float | None = Field(None, description="Цена варианта (руб., число)")


class OzonAspectValue(BaseModel):
    """Одно значение переключателя вариантов: свой sku, доступность и цены."""
    sku: str | None = Field(None, description="Артикул этого значения")
    value: str | None = Field(None, description="Подпись значения (например, 'розовый', '128 RU / 128')")
    availability: str | None = Field(None, description="Доступность (inStock/outOfStock)")
    price: float | None = Field(None, description="Цена (руб., число)")
    original_price: float | None = Field(None, description="Цена без скидки (руб., число)")
    active: bool = Field(False, description="Значение открытой карточки")


class OzonAspect(BaseModel):
    """Переключатель вариантов из виджета webAspects (цвет, размер и т. п.)."""
    name: str | None = Field(None, description="Название переключателя ('Цвет', 'Размер')")
    key: str | None = Field(None, description="Ключ Ozon ('Color', 'size')")
    type: str | None = Field(None, description="Тип переключателя ('colorsMix', 'sizes')")
    values: list[OzonAspectValue] = Field(default_factory=list, description="Значения переключателя")


class OzonCard(BaseModel):
    """
    Агрегированная карточка товара Ozon.

    Поля, которые не удалось найти на странице, остаются None — это нормально,
    набор виджетов у разных товаров отличается.
    """
    # Идентификация
    sku: str | None = Field(None, description="Артикул товара")
    url: str | None = Field(None, description="URL карточки")
    parsed_at: str | None = Field(None, description="Дата и время парсинга (ISO)")
    name: str | None = Field(None, description="Наименование товара")
    brand: str | None = Field(None, description="Бренд")

    # Цены
    price: OzonPrice = Field(default_factory=OzonPrice)
    quantity: int | None = Field(None, description="Количество/остаток, если указано")

    # Оценки
    rating: str | None = Field(None, description="Рейтинг (например, '4.9')")
    reviews_count: str | None = Field(None, description="Число отзывов (например, '103')")

    # Характеристики (плоский словарь: название -> значение).
    # Сюда попадают сезон, материал, состав материала, коллекция, рост, размер и пр.
    characteristics: dict[str, str] = Field(
        default_factory=dict,
        description="Краткие характеристики товара: {название: значение}",
    )

    # Полный блок характеристик (webCharacteristics) — все пары в порядке страницы,
    # без отбора. Пусто, если блока нет в разметке.
    characteristics_full: dict[str, str] = Field(
        default_factory=dict,
        description="Полные характеристики товара: {название: значение}",
    )

    # Код ТН ВЭД (только цифры) — из полного блока характеристик, если Ozon его
    # показывает. Пусто, если на странице кода нет.
    tnved: str | None = Field(None, description="Код ТН ВЭД (цифры)")

    # Описание и категория
    description: str | None = Field(None, description="Текстовое описание товара")
    category: str | None = Field(None, description="Категория")
    category_path: str | None = Field(None, description="Путь категории (иерархия)")

    # Варианты (размерная сетка): какой аспект и список вариантов с доступностью.
    variants_aspect: str | None = Field(None, description="По чему варианты (напр. 'Размер')")
    variants: list[OzonVariant] = Field(
        default_factory=list,
        description="Список вариантов (размеров) с доступностью, ценой и sku",
    )

    # Все переключатели вариантов (цвет, размер и т. п.), каждый со своими sku.
    aspects: list[OzonAspect] = Field(
        default_factory=list,
        description="Все переключатели вариантов из webAspects",
    )

    # Признак полного разбора: в разметке найден полный блок характеристик.
    full_content: bool = Field(False, description="Полный блок характеристик собран")

    # Продавец
    seller: OzonSeller = Field(default_factory=OzonSeller)

    # Регион выдачи (город/страна, в контексте которых отданы цены и наличие)
    location: OzonLocation = Field(default_factory=OzonLocation)

    # Состояние товара по признакам страницы:
    # in_cart — «В корзину» (в наличии); out_of_stock — нет в наличии;
    # not_found — страницы не существует; unknown — не распозналось.
    button_state: str | None = Field(None, description="Состояние кнопки покупки")