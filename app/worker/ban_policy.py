# -*- coding: utf-8 -*-
"""
ban_policy.py — расчёт паузы после бана (чистые функции, без Redis и браузера).

Уровень растёт на 1 с каждым баном; пауза = start × 2^(уровень−1), не больше max.
Уровень обнуляется, если с конца прошлой паузы прошло reset_s секунд без бана.
"""


def next_ban_level(prev_level: int, prev_until: float, now: float, reset_s: float) -> int:
    """Уровень нового бана. prev_until — конец прошлой паузы (0, если банов не было)."""
    if prev_level <= 0 or prev_until <= 0 or now - prev_until >= reset_s:
        return 1
    return prev_level + 1


def ban_pause_s(level: int, start_s: float, max_s: float) -> float:
    """Длительность паузы для уровня 1, 2, 3, ..."""
    return min(start_s * (2 ** (max(level, 1) - 1)), max_s)