# -*- coding: utf-8 -*-
"""Выбор свободного дисплея Xvfb."""
from app.marketplaces.ozon.display import find_free_display


def test_first_free_display(tmp_path):
    x11 = tmp_path / "x11"
    x11.mkdir()
    (x11 / "X100").write_text("")            # занят сокетом
    (tmp_path / ".X101-lock").write_text("")  # занят lock-файлом
    assert find_free_display(100, 110, str(x11), str(tmp_path)) == 102