# -*- coding: utf-8 -*-
"""Расчёт уровня бана и длительности паузы."""
from app.worker.ban_policy import ban_pause_s, next_ban_level

DAY = 86400.0


def test_pause_sequence_with_cap():
    pauses = [ban_pause_s(level, 600.0, DAY) for level in range(1, 11)]
    assert pauses == [600, 1200, 2400, 4800, 9600, 19200, 38400, 76800, DAY, DAY]


def test_first_ban_level_one():
    assert next_ban_level(0, 0.0, now=1000.0, reset_s=DAY) == 1


def test_level_grows_within_reset_window():
    assert next_ban_level(3, prev_until=1000.0, now=1000.0 + 3600, reset_s=DAY) == 4


def test_level_resets_after_day_without_ban():
    assert next_ban_level(5, prev_until=1000.0, now=1000.0 + DAY, reset_s=DAY) == 1