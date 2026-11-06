# -*- coding: utf-8 -*-
"""
test_mapper_colon_keys.py — названия характеристик без завершающего двоеточия.

Ozon у части товаров пишет «Рисунок:», «Особенности модели:» вместо «Рисунок»,
«Особенности модели». Название чистится при разборе: в базу уходит один вариант.
Работают на синтетической разметке и сохранённых страницах, без обращения к Ozon.
"""

import html as htmllib
import json
import os

from app.marketplaces.ozon.mapper import (
    build_card, _extract_characteristics, _extract_characteristics_full,
)
from app.marketplaces.ozon.nuxt import extract_nuxt_state

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _full_html(pairs: list[tuple[str, str]]) -> str:
    items = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in pairs)
    return f'<div data-widget="webCharacteristics"><dl>{items}</dl></div>'


def _short_html(title: str, value: str) -> str:
    state = {"characteristics": [{
        "title": {"textRs": [{"type": "text", "content": title}]},
        "values": [{"text": value}],
    }]}
    data = htmllib.escape(json.dumps(state, ensure_ascii=False))
    return f'<div id="state-webShortCharacteristics-1-default-1" data-state="{data}"></div>'


def test_full_trailing_colon_removed():
    html = _full_html([("Рисунок:", "полоска"), ("Цвет", "розовый")])
    assert _extract_characteristics_full(html) == {"Рисунок": "полоска", "Цвет": "розовый"}


def test_full_colon_after_space_removed():
    html = _full_html([("Состав материала :", "хлопок 100%")])
    assert _extract_characteristics_full(html) == {"Состав материала": "хлопок 100%"}


def test_full_both_variants_same_value_not_duplicated():
    html = _full_html([("Рисунок", "полоска"), ("Рисунок:", "полоска")])
    assert _extract_characteristics_full(html) == {"Рисунок": "полоска"}


def test_full_both_variants_different_values_joined():
    html = _full_html([("Рисунок", "полоска"), ("Рисунок:", "клетка")])
    assert _extract_characteristics_full(html) == {"Рисунок": "полоска, клетка"}


def test_full_key_of_only_colon_skipped():
    html = _full_html([(":", "что-то"), ("Цвет", "розовый")])
    assert _extract_characteristics_full(html) == {"Цвет": "розовый"}


def test_short_title_colon_removed():
    assert _extract_characteristics(_short_html("Сезон:", "Демисезон")) == {"Сезон": "Демисезон"}


def test_fixture_cards_have_no_colon_keys():
    for name in ("ozon_plate_zolla_4439072345.html", "ozon_longsliv_happyfox_4569427009.html"):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
            html = f.read()
        card = build_card(html, extract_nuxt_state(html), "https://www.ozon.ru/product/x-1/")
        keys = list(card.characteristics) + list(card.characteristics_full)
        assert keys
        assert not [k for k in keys if k.endswith(":") or k != k.strip()]