# -*- coding: utf-8 -*-
"""Марки Client Hints по алгоритму Chromium — сверка с реальными значениями Chrome."""
from app.marketplaces.ozon.parser import chrome_brands


def _pairs(brands):
    return [(b["brand"], b["version"]) for b in brands]


def test_chrome_120():
    brands, _ = chrome_brands("120.0.6099.109")
    assert _pairs(brands) == [("Not_A Brand", "8"), ("Chromium", "120"), ("Google Chrome", "120")]


def test_chrome_124():
    brands, _ = chrome_brands("124.0.6367.60")
    assert _pairs(brands) == [("Chromium", "124"), ("Google Chrome", "124"), ("Not-A.Brand", "99")]


def test_chrome_131():
    brands, _ = chrome_brands("131.0.6778.85")
    assert _pairs(brands) == [("Google Chrome", "131"), ("Chromium", "131"), ("Not_A Brand", "24")]


def test_full_version_list():
    _, full_list = chrome_brands("152.0.7977.64")
    assert _pairs(full_list) == [("Chromium", "152.0.7977.64"), ("Not?A_Brand", "24.0.0.0"),
                                 ("Google Chrome", "152.0.7977.64")]