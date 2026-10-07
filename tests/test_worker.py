# -*- coding: utf-8 -*-
"""
test_worker.py — тесты логики Kafka-воркера БЕЗ реального брокера.

Проверяют обработчики select/parse: сборку результата, эхо-поля, ok/error,
разбор задачи. Kafka и Selenium не нужны — сервис замокан.

Запуск: pytest tests/test_worker.py -v
"""

import pytest

from app.marketplaces.ozon.selection import Collection
from app.worker.handlers import handle_select, handle_parse, _build_stratum_request

import sys, types
# Заглушка сервиса, чтобы тест не тянул Selenium в CI
if "app.marketplaces.ozon.service" not in sys.modules:
    _m = types.ModuleType("app.marketplaces.ozon.service")
    _m.OzonService = object
    sys.modules["app.marketplaces.ozon.service"] = _m

# ------------------------------------------------------------------ #
#  Мок сервиса (вместо реального OzonService с Selenium)             #
# ------------------------------------------------------------------ #
class _FakeCard:
    def __init__(self, sku):
        self.sku = sku

    def model_dump(self):
        return {"sku": self.sku, "name": "Рубашка"}


class FakeService:
    """Мок OzonService: успех или сбой по флагу."""
    def __init__(self, fail=False):
        self.fail = fail

    def select_for_stratum(self, req):
        if self.fail:
            raise RuntimeError("сбой подбора")
        return {"cards": [_FakeCard("1"), _FakeCard("2")],
                "requested": req.count, "found": 2}

    def get_card_for_crawl_by_id(self, sku, full_content=False):
        if self.fail:
            raise RuntimeError("страница не грузится")
        return {"card": {"sku": sku, "name": "Рубашка Zella"}, "debug_files": None}


# ------------------------------------------------------------------ #
#  SELECT                                                            #
# ------------------------------------------------------------------ #
def _select_task():
    return {
        "task_id": "T1", "set_id": "S1", "stratum_id": "ST1", "geo": "Санкт-Петербург",
        "query": "Рубашка женская", "count": 2, "is_seasonal": False,
    }


def test_select_success_echo_fields():
    r = handle_select(_select_task(), FakeService())
    # эхо-поля вернулись без изменений
    assert r["task_id"] == "T1"
    assert r["set_id"] == "S1"
    assert r["stratum_id"] == "ST1"
    assert r["geo"] == "Санкт-Петербург"


def test_select_success_structure():
    r = handle_select(_select_task(), FakeService())
    assert r["ok"] is True
    assert r["requested_count"] == 2
    assert r["found_count"] == 2
    assert len(r["cards"]) == 2
    assert r["cards"][0]["sku"] == "1"   # OzonCard сериализован в dict
    assert r["error"] is None


def test_select_failure():
    r = handle_select(_select_task(), FakeService(fail=True))
    assert r["ok"] is False
    assert r["cards"] == []
    assert r["error"]                     # текст ошибки присутствует
    # эхо-поля сохранены даже при ошибке
    assert r["task_id"] == "T1"
    assert r["set_id"] == "S1"


def test_select_builds_request_with_exclude():
    # collection в контракте — enum Collection (base/spring_summer/autumn_winter),
    # ядро присылает именно значение enum, а не название коллекции с карточки.
    task = {
        "query": "q", "count": 3, "is_seasonal": True, "base_share": 0.5,
        "exclude": [{
            "sku": "9", "name": "n", "url": "u",
            "seller": "554625", "collection": "spring_summer",
        }],
    }
    req = _build_stratum_request(task)
    assert req.query == "q" and req.count == 3
    assert req.is_seasonal is True and req.base_share == 0.5
    assert len(req.exclude) == 1
    # поле seller проброшено (не brand!) — соответствие контракту
    assert req.exclude[0].seller == "554625"
    # collection распарсилась в enum
    assert req.exclude[0].collection == Collection.SPRING_SUMMER


# ------------------------------------------------------------------ #
#  PARSE                                                             #
# ------------------------------------------------------------------ #
def _parse_task():
    return {"task_id": "T2", "card_id": "C2", "sku": "4163755961", "geo": "Санкт-Петербург"}


def test_parse_success():
    r = handle_parse(_parse_task(), FakeService())
    assert r["task_id"] == "T2"
    assert r["card_id"] == "C2"
    assert r["sku"] == "4163755961"
    assert r["geo"] == "Санкт-Петербург"
    assert r["ok"] is True
    assert r["card"]["sku"] == "4163755961"
    assert r["error"] is None


def test_parse_failure():
    r = handle_parse(_parse_task(), FakeService(fail=True))
    assert r["ok"] is False
    assert r["card"] is None
    assert r["error"]
    assert r["card_id"] == "C2"           # эхо сохранено


def test_parse_missing_sku():
    r = handle_parse({"task_id": "T3", "card_id": "C3", "geo": "spb"}, FakeService())
    assert r["ok"] is False
    assert r["card"] is None
    assert "sku" in r["error"].lower()