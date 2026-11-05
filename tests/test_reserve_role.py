# -*- coding: utf-8 -*-
"""Роль reserve: задачи обхода только по сигналу ядра; primary сигнал не смотрит
и слушает все очереди (обход первым)."""
from types import SimpleNamespace

from app.marketplaces.ozon.service import CRAWL, SEARCH
from app.worker import consumer as consumer_module
from app.worker.consumer import LaneState, TaskConsumer
from app.worker.redis_config import redis_worker_settings as rws

POLICY = SimpleNamespace(load_failure_threshold=3, ban_pause_start_s=600.0,
                         ban_pause_max_s=7200.0, ban_level_reset_s=21600.0,
                         search_idle_close_s=900.0, crawl_between_select=20)


class FakeService:
    """Сервис без браузеров: цикл воркера после задачи проверяет простой выдачи."""

    def close_search_if_idle(self, idle_s):
        return False


class FakeRedis:
    def __init__(self, queues, signal=False):
        self.queues = queues
        self.signal = signal
        self.brpop_calls = []
        self.exists_calls = []

    def exists(self, key):
        self.exists_calls.append(key)
        return 1 if self.signal else 0

    def brpop(self, keys, timeout=0):
        self.brpop_calls.append(list(keys))
        for key in keys:
            if self.queues.get(key):
                return key, self.queues[key].pop()
        return None


def make_consumer(redis):
    c = TaskConsumer.__new__(TaskConsumer)
    c._policy = POLICY
    c._running = True
    c._lanes = {CRAWL: LaneState(), SEARCH: LaneState()}
    c._search_paused = False
    c._reserve_active = False
    c._probe_pending = False
    c._generations = {CRAWL: 0, SEARCH: 0}
    c._task_keys = consumer_module._queue_order(rws.task_keys, rws.key_routes, select_turn=False)
    c._turn_keys = consumer_module._queue_order(rws.task_keys, rws.key_routes, select_turn=True)
    c._parse_streak = 0
    c.service = FakeService()
    c.redis = redis
    return c


def _queues():
    return {rws.select_tasks_key("ozon"): ["{}"], rws.search_tasks_key("ozon"): ["{}"],
            rws.parse_tasks_key("ozon"): ["{}"]}


def _run_until_task(c, max_sleeps=1):
    """Цикл до первой взятой задачи или до max_sleeps пауз ожидания сигнала."""
    taken, sleeps = [], []

    def process(key, raw):
        taken.append(key)
        c._running = False

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= max_sleeps:
            c._running = False

    c._process_one = process
    c._interruptible_sleep = sleep
    c._consume_loop()
    return taken, sleeps


def test_signal_key_format():
    assert rws.reserve_signal_key("ozon") == f"monitoring:reserve:ozon:{rws.geo_code}"


def test_reserve_without_signal_takes_nothing(monkeypatch):
    monkeypatch.setattr(rws, "role", "reserve")
    redis = FakeRedis(_queues(), signal=False)
    taken, sleeps = _run_until_task(make_consumer(redis))
    assert taken == [] and sleeps == [consumer_module.RESERVE_POLL_S]
    assert redis.brpop_calls == []
    assert redis.exists_calls == [rws.reserve_signal_key("ozon")]


def test_reserve_with_signal_takes_only_parse(monkeypatch):
    monkeypatch.setattr(rws, "role", "reserve")
    redis = FakeRedis(_queues(), signal=True)
    c = make_consumer(redis)
    taken, _ = _run_until_task(c)
    assert taken == [rws.parse_tasks_key("ozon")]
    assert redis.brpop_calls == [rws.parse_task_keys]   # резерв слушает только обход
    assert c._reserve_active is True


def test_primary_ignores_signal(monkeypatch):
    monkeypatch.setattr(rws, "role", "primary")
    redis = FakeRedis(_queues(), signal=False)
    c = make_consumer(redis)
    taken, _ = _run_until_task(c)
    assert taken == [rws.parse_tasks_key("ozon")]        # обход первым
    assert redis.brpop_calls == [c._task_keys]           # слушает все очереди
    assert redis.exists_calls == []


def test_is_reserve_case_insensitive(monkeypatch):
    monkeypatch.setattr(rws, "role", " Reserve ")
    assert rws.is_reserve is True
    monkeypatch.setattr(rws, "role", "primary")
    assert rws.is_reserve is False