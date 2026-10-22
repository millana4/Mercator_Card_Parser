# -*- coding: utf-8 -*-
"""Устаревшие задачи выдачи не выполняются; значения пауз по умолчанию."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.config import settings
from app.worker.consumer import TaskConsumer
from app.worker.handlers import handle_search
from app.worker.redis_config import redis_worker_settings as rws

TASK = {"task_id": "t-1", "set_id": "s-1", "stratum_id": "st-1", "geo": "Санкт-Петербург",
        "marketplace": "ozon", "category": "kids_clothing", "query": "Брюки для мальчика"}


class FakeService:
    def __init__(self, max_age=6 * 3600):
        self.policy = SimpleNamespace(search_task_max_age_s=max_age)
        self.calls = 0

    def search_tiles(self, query, target=None, save_files=True):
        self.calls += 1
        return {"url": "u", "scrolls": 1, "tiles_total": 0, "tiles": []}


def _run_at(hours_ago: float) -> str:
    return (datetime.now(timezone(timedelta(hours=3))) - timedelta(hours=hours_ago)).isoformat()


def test_stale_task_skipped_without_ozon():
    svc = FakeService()
    r = handle_search({**TASK, "run_at": _run_at(7)}, svc)
    assert r["ok"] is False and r["error_code"] == "expired" and r["_failure"] == "expired"
    assert svc.calls == 0


def test_fresh_task_executed():
    svc = FakeService()
    r = handle_search({**TASK, "run_at": _run_at(1)}, svc)
    assert r["ok"] is True and svc.calls == 1


def test_task_without_run_at_executed():
    svc = FakeService()
    assert handle_search(TASK, svc)["ok"] is True and svc.calls == 1


def test_expired_result_no_counters_no_pause():
    consumer = TaskConsumer.__new__(TaskConsumer)
    consumer._routes = rws.key_routes
    sent, counters, tracked, paused = [], [], [], []
    consumer.producer = SimpleNamespace(send=lambda k, r: sent.append(r),
                                        incr_counter=counters.append)
    consumer.service = FakeService()
    consumer._track_failure = lambda *a: tracked.append(a)
    consumer._pause_between_cards = lambda: paused.append(1)
    consumer._process_one(rws.search_tasks_key("ozon"), json.dumps({**TASK, "run_at": _run_at(8)}))
    assert len(sent) == 1 and sent[0]["error_code"] == "expired" and "_failure" not in sent[0]
    assert counters == [] and tracked == [] and paused == []


def test_policy_defaults(monkeypatch):
    for key in ("OZON_BAN_PAUSE_MAX_S", "OZON_BAN_LEVEL_RESET_S", "OZON_SEARCH_TASK_MAX_AGE_S"):
        monkeypatch.delenv(key, raising=False)
    p = settings.ozon_policy()
    assert (p.ban_pause_max_s, p.ban_level_reset_s, p.search_task_max_age_s) == (7200, 21600, 21600)