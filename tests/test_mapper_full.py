# -*- coding: utf-8 -*-
"""
test_mapper_full.py — тесты полного разбора карточки.

Проверяют то, что нужно для подбора аналогов в нишах:
  - полный блок характеристик (webCharacteristics) — все пары, без отбора;
  - описание (JSON-LD, запасной источник — разметка webDescription);
  - все переключатели вариантов (webAspects): цвет и размер, у каждого значения свой sku;
  - признак full_content.
Старые поля (characteristics, variants_aspect, variants) не должны меняться.

Фикстура ozon_longsliv_happyfox_4569427009.html — страница лонгслива Happyfox,
сохранённая парсером 06.10.2026.

Запуск:  python -m pytest tests/test_mapper_full.py -v
"""

import os

from app.marketplaces.ozon.mapper import build_card
from app.marketplaces.ozon.nuxt import extract_nuxt_state

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
LONGSLIV = os.path.join(FIXTURES, "ozon_longsliv_happyfox_4569427009.html")
LONGSLIV_URL = "https://www.ozon.ru/product/longsliv-happyfox-dlya-devochek-1-sht-4569427009/"
ZOLLA = os.path.join(FIXTURES, "ozon_plate_zolla_4439072345.html")
ZOLLA_URL = "https://www.ozon.ru/product/plate-zolla-4439072345/"
ACTIVE_SKU = "4569427009"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _card(path: str, url: str, with_nuxt: bool = True):
    html = _read(path)
    nuxt = extract_nuxt_state(html) if with_nuxt else None
    return build_card(html, nuxt, url)


# ---------- Полный блок характеристик ----------

def test_characteristics_full_all_pairs():
    card = _card(LONGSLIV, LONGSLIV_URL)
    keys = list(card.characteristics_full)
    assert len(keys) == 23
    assert keys[0] == "Артикул"
    assert keys[-1] == "Уход за вещами"
    assert card.characteristics_full["Тип"] == "Лонгслив"
    assert card.characteristics_full["Покрой"] == "облегающий"
    assert card.characteristics_full["Тип рукава"] == "Длинный"
    assert card.characteristics_full["Российский размер"] == "140"


def test_characteristics_full_link_values_joined():
    card = _card(LONGSLIV, LONGSLIV_URL)
    # <a>В рубчик</a><span>, </span>Рюши — без лишнего пробела перед запятой
    assert card.characteristics_full["Декоративные элементы"] == "В рубчик, Рюши"
    assert card.characteristics_full["Страна-изготовитель"] == "Россия"


def test_characteristics_full_raw_values():
    card = _card(LONGSLIV, LONGSLIV_URL)
    # Значения хранятся как на странице, без нормализации (латинская «x» у продавца)
    assert card.characteristics_full["Состав материала"] == "xлопок 95%, лайкра 5%"


def test_short_characteristics_unchanged():
    card = _card(LONGSLIV, LONGSLIV_URL)
    assert len(card.characteristics) == 5
    assert set(card.characteristics) <= set(card.characteristics_full)


def test_full_content_flag():
    card = _card(LONGSLIV, LONGSLIV_URL)
    assert card.full_content is True


# ---------- Переключатели вариантов ----------

def test_aspects_color_and_size():
    card = _card(LONGSLIV, LONGSLIV_URL)
    assert [a.name for a in card.aspects] == ["Цвет", "Размер"]
    assert [a.key for a in card.aspects] == ["Color", "size"]
    assert [a.type for a in card.aspects] == ["colorsMix", "sizes"]
    color, size = card.aspects
    assert len(color.values) == 3
    assert len(size.values) == 7
    assert all(v.sku for v in color.values + size.values)


def test_aspects_active_value():
    card = _card(LONGSLIV, LONGSLIV_URL)
    for aspect in card.aspects:
        active = [v for v in aspect.values if v.active]
        assert len(active) == 1
        assert active[0].sku == ACTIVE_SKU


def test_aspects_values_and_prices():
    card = _card(LONGSLIV, LONGSLIV_URL)
    color, size = card.aspects
    assert [v.value for v in color.values] == ["белый", "голубой", "розовый"]
    first_size = size.values[0]
    assert first_size.value == "128 RU / 128"
    assert first_size.availability == "inStock"
    assert first_size.price == 728.0
    assert first_size.original_price == 1079.0


def test_old_variants_unchanged():
    card = _card(LONGSLIV, LONGSLIV_URL)
    # Старое поле — по-прежнему первый переключатель
    assert card.variants_aspect == "Цвет"
    assert len(card.variants) == 3


# ---------- Описание ----------

def test_description_from_ld_json():
    card = _card(LONGSLIV, LONGSLIV_URL)
    assert card.description.startswith("Нежный розовый лонгслив для девочки")
    assert "акцент в школьном" in card.description
    assert "\n" in card.description
    assert card.description.endswith("выпускного.")


def test_description_fallback_from_markup():
    card = _card(LONGSLIV, LONGSLIV_URL, with_nuxt=False)
    assert card.description.startswith("Нежный розовый лонгслив")
    assert "Показать полностью" not in card.description
    assert "выпускного" in card.description


# ---------- Страница без блоков ----------

def test_page_without_widgets():
    card = build_card("<html><body></body></html>", None, LONGSLIV_URL)
    assert card.characteristics_full == {}
    assert card.aspects == []
    assert card.full_content is False


# ---------- Вторая фикстура ----------

def test_zolla_full_block():
    card = _card(ZOLLA, ZOLLA_URL)
    assert card.full_content is True
    assert set(card.characteristics) <= set(card.characteristics_full)




# ---------- ТН ВЭД ----------

def test_tnved_absent_on_page():
    card = _card(LONGSLIV, LONGSLIV_URL)
    assert card.tnved is None


def test_tnved_from_characteristics():
    html = ('<div data-widget="webCharacteristics"><dl><dt>Код ТН ВЭД</dt>'
            '<dd>6109 10 000 0</dd></dl></div>')
    card = build_card(html, None, LONGSLIV_URL)
    assert card.tnved == "6109100000"
    assert card.characteristics_full["Код ТН ВЭД"] == "6109 10 000 0"