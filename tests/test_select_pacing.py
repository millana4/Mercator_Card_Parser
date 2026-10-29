# -*- coding: utf-8 -*-
"""Пауза между переходами внутри задачи подбора (NavigationPacer).
Паузы самого подбора — в test_select_browsers.py."""
from app.marketplaces.ozon.service import NavigationPacer


def test_pacer_first_wait_without_pause():
    calls = []
    pacer = NavigationPacer(50.0, 90.0, sleep=calls.append)
    pacer.wait()
    assert calls == []


def test_pacer_next_waits_in_range():
    calls = []
    pacer = NavigationPacer(50.0, 90.0, sleep=calls.append)
    for _ in range(4):
        pacer.wait()
    assert len(calls) == 3
    assert all(50.0 <= d <= 90.0 for d in calls)