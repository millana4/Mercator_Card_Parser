# -*- coding: utf-8 -*-
"""
test_mapper_image.py — адрес главного фото карточки (image_url).

Источник — meta og:image (вариант 600 px), запасной — image из товарного JSON-LD.
Картинки не товара (логотип Ozon на странице поиска) отбрасываются.
Работают на сохранённом HTML и синтетических фрагментах, без обращения к Ozon.
"""

import json
import os

from app.marketplaces.ozon.mapper import build_card, _extract_image_url
from app.marketplaces.ozon.nuxt import extract_nuxt_state

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _ld_nuxt(image) -> dict:
    """__NUXT__.state с товарным JSON-LD, где поле image = image."""
    ld = {"@context": "https://schema.org", "@type": "Product", "image": image}
    return {"seo": {"script": [{"innerHTML": json.dumps(ld)}]}}


def test_plate_fixture_card_gets_og_image():
    html = _read("ozon_plate_zolla_4439072345.html")
    card = build_card(html, extract_nuxt_state(html),
                      "https://www.ozon.ru/product/plate-zolla-4439072345/")
    assert card.image_url == "https://ir.ozone.ru/s3/multimedia-1-k/c600/10793471420.jpg"


def test_longsliv_fixture_og_image():
    html = _read("ozon_longsliv_happyfox_4569427009.html")
    assert _extract_image_url(html, extract_nuxt_state(html)) == \
        "https://ir.ozone.ru/s3/multimedia-1-m/c600/14983784302.jpg"


def test_search_page_logo_is_rejected():
    html = _read("ozon_search_trusy_zhenskie.html")
    assert _extract_image_url(html, extract_nuxt_state(html)) is None


def test_content_attribute_before_property():
    html = '<head><meta content="https://ir.ozone.ru/s3/multimedia-1-a/c600/1.jpg" property="og:image"></head>'
    assert _extract_image_url(html, None) == "https://ir.ozone.ru/s3/multimedia-1-a/c600/1.jpg"


def test_protocol_relative_url_and_entities():
    html = '<meta property="og:image" content="//ir.ozone.ru/s3/multimedia-1-a/c600/2.jpg?a=1&amp;b=2">'
    assert _extract_image_url(html, None) == "https://ir.ozone.ru/s3/multimedia-1-a/c600/2.jpg?a=1&b=2"


def test_json_ld_fallback_takes_first_of_list():
    nuxt = _ld_nuxt(["https://ir.ozone.ru/s3/multimedia-1-b/3.jpg",
                     "https://ir.ozone.ru/s3/multimedia-1-b/4.jpg"])
    assert _extract_image_url("<html></html>", nuxt) == "https://ir.ozone.ru/s3/multimedia-1-b/3.jpg"


def test_json_ld_fallback_image_object():
    nuxt = _ld_nuxt({"@type": "ImageObject", "url": "https://ir.ozone.ru/s3/multimedia-1-c/5.jpg"})
    assert _extract_image_url("<html></html>", nuxt) == "https://ir.ozone.ru/s3/multimedia-1-c/5.jpg"


def test_logo_in_og_and_no_json_ld_gives_none():
    html = '<meta property="og:image" content="https://ir.ozone.ru/s3/cms/logo/og_ozon_ru.png">'
    assert _extract_image_url(html, {}) is None


def test_no_image_anywhere():
    assert _extract_image_url("<html></html>", None) is None
    assert _extract_image_url("", {}) is None