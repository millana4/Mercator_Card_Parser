# -*- coding: utf-8 -*-
"""Время жизни задачи подбора: созданная давно задача не выполняется (ответ expired,
воркер блокировкой не считает); свежая и без created_at - выполняются как обычно."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.worker.handlers import handle_select

MAX_AGE_S = 6 * 3600.0


class Service:
    policy = SimpleNamespace(select_task_max_age_s=MAX_AGE_S)

    def __init__(self):
        self.calls = 0

    def select_for_stratum(self, req):
        self.calls += 1
        return {"cards": [], "requested": req.count, "found": 0, "blocked": None}


def _task(created_at=None):
    task = {"task_id": "t", "stratum_id": "s", "query": "юбка для девочки", "count": 2}
    if created_at is not None:
        task["created_at"] = created_at.isoformat()
    return task


def test_old_task_is_not_run():
    service = Service()
    old = datetime.now(timezone.utc) - timedelta(seconds=MAX_AGE_S + 3600)
    result = handle_select(_task(old), service)
    assert service.calls == 0
    assert result["ok"] is False and result["error_code"] == "expired"
    assert result["_failure"] == "expired" and result["found_count"] == 0


def test_fresh_task_runs():
    service = Service()
    fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
    result = handle_select(_task(fresh), service)
    assert service.calls == 1 and result["ok"] is True


def test_task_without_created_at_runs():
    service = Service()
    result = handle_select(_task(), service)
    assert service.calls == 1 and result["ok"] is True