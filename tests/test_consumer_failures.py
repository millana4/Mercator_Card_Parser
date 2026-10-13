# -*- coding: utf-8 -*-
"""Учёт блокировок воркером: бан сразу, серия сбоев загрузки, уровень паузы
и поколение устройства в Redis."""
import time
from types import SimpleNamespace

from app.core.exceptions import AntibotBlockedError, PageLoadError, ProductNotFoundError
from app.worker.consumer import TaskConsumer
from app.worker.handlers import _error_code, _failure_kind
from app.worker.redis_config import redis_worker_settings as rws

POLICY = SimpleNamespace(load_failure_threshold=3, ban_pause_start_s=600.0,
                         ban_pause_max_s=86400.0, ban_level_reset_s=86400.0)


class FakeService:
    def __init__(self):
        self.generations = []
        self.resets = 0

    def set_device_generation(self, generation):
        self.generations.append(generation)

    def reset_profile(self):
        self.resets += 1


class FakeRedis:
    def __init__(self):
        self.data = {}

    def hgetall(self, key):
        return {k: str(v) for k, v in self.data.get(key, {}).items()}

    def hset(self, key, mapping):
        self.data[key] = dict(mapping)


def make_consumer():
    consumer = TaskConsumer.__new__(TaskConsumer)
    consumer._policy = POLICY
    consumer._load_failures = 0
    consumer._banned_until = 0.0
    return consumer


def test_failure_kind_and_code():
    assert _failure_kind(PageLoadError("x")) == "load"
    assert _failure_kind(AntibotBlockedError("x")) == "ban"
    assert _failure_kind(ProductNotFoundError("x")) is None
    assert _error_code(PageLoadError("x")) == "antibot_blocked"


def test_ban_registered_immediately():
    consumer = make_consumer()
    bans = []
    consumer._register_ban = lambda mp, reason: bans.append(reason)
    consumer._track_failure("ban", "ozon")
    assert len(bans) == 1


def test_load_failures_threshold_and_reset():
    consumer = make_consumer()
    bans = []
    consumer._register_ban = lambda mp, reason: bans.append(reason)
    consumer._track_failure("load", "ozon")
    consumer._track_failure("load", "ozon")
    consumer._track_failure(None, "ozon")       # страница загрузилась — серия обнулена
    consumer._track_failure("load", "ozon")
    consumer._track_failure("load", "ozon")
    assert bans == []
    consumer._track_failure("load", "ozon")
    assert len(bans) == 1


def test_register_ban_levels_generation_and_profile_reset():
    consumer = make_consumer()
    consumer.redis = FakeRedis()
    consumer.service = FakeService()

    start = time.time()
    consumer._register_ban("ozon", "капча")
    state = consumer.redis.data[rws.ban_state_key("ozon")]
    assert state["level"] == 1
    assert state["generation"] == 1
    assert 590 <= state["until"] - start <= 610

    consumer._register_ban("ozon", "капча")
    state = consumer.redis.data[rws.ban_state_key("ozon")]
    assert state["level"] == 2
    assert state["generation"] == 2
    assert 1190 <= state["until"] - start <= 1210
    assert consumer.service.generations == [1, 2]
    assert consumer.service.resets == 2


def test_load_ban_state_restores_generation():
    consumer = make_consumer()
    consumer.redis = FakeRedis()
    consumer.service = FakeService()
    consumer.redis.hset(rws.ban_state_key(rws.marketplaces[0]),
                        {"level": 3, "until": time.time() - 10, "generation": 4})
    consumer._load_ban_state()
    assert consumer.service.generations == [4]
    assert consumer._banned_until == 0.0        # пауза уже закончилась