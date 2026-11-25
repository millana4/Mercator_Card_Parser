# ---------- Бренд из JSON-LD: строка, объект schema.org, список ----------
import pytest

from app.marketplaces.ozon import mapper


@pytest.mark.parametrize("raw, expected", [
    ("Zolla", "Zolla"),
    ("  Zolla  ", "Zolla"),
    ({"@type": "Brand", "name": "Flycoon"}, "Flycoon"),
    ({"@type": "Brand", "name": "  Flycoon "}, "Flycoon"),
    ({"@type": "Brand"}, None),
    ({"@type": "Brand", "name": ""}, None),
    ([{"@type": "Brand", "name": "Flycoon"}, "Zolla"], "Flycoon"),
    (["", {"@type": "Brand", "name": "Zolla"}], "Zolla"),
    ([], None),
    (123, None),
])
def test_extract_brand_forms(monkeypatch, raw, expected):
    monkeypatch.setattr(mapper, "_ld_json", lambda nuxt: {"brand": raw})
    assert mapper._extract_brand("", {}) == expected


def test_extract_brand_without_ld(monkeypatch):
    monkeypatch.setattr(mapper, "_ld_json", lambda nuxt: None)
    assert mapper._extract_brand("", {}) is None
    monkeypatch.setattr(mapper, "_ld_json", lambda nuxt: {})
    assert mapper._extract_brand("", {}) is None