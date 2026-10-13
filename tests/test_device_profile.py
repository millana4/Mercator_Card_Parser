# -*- coding: utf-8 -*-
"""Наборы устройств и выбор набора по парсеру и поколению."""
import re

from app.marketplaces.ozon.device_profile import DEVICE_PROFILES, TASKBAR_HEIGHT, pick_device


def test_pick_is_deterministic():
    assert pick_device("5", 0) == pick_device("5", 0)


def test_neighbour_parsers_differ():
    assert pick_device("5", 0) != pick_device("6", 0)


def test_generation_moves_to_next_device_and_cycles():
    n = len(DEVICE_PROFILES)
    seen = {pick_device("1", g).name for g in range(n)}
    assert len(seen) == n
    assert pick_device("1", n) == pick_device("1", 0)


def test_non_numeric_parser_id():
    assert pick_device("home-pc", 0) in DEVICE_PROFILES


def test_profiles_consistent():
    pattern = re.compile(r"^ANGLE \((Intel|NVIDIA|AMD), .+ \(0x[0-9A-F]{8}\) "
                         r"Direct3D11 vs_5_0 ps_5_0, D3D11\)$")
    for d in DEVICE_PROFILES:
        m = pattern.match(d.webgl_renderer)
        assert m, d.webgl_renderer
        assert d.webgl_vendor == f"Google Inc. ({m.group(1)})"
        assert d.avail_height == d.height - TASKBAR_HEIGHT
        assert d.memory in (2, 4, 8, 16, 32)