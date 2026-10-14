# -*- coding: utf-8 -*-
"""Учёт блокировок воркером: бан сразу, серия сбоев загрузки, уровень и пауза
в памяти процесса, сброс профиля и устройства только у сессии бана,
пробная задача после паузы — из очереди обхода."""
import time
from types import SimpleNamespace

from app.core.exceptions import AntibotBlockedError, PageLoadError, ProductNotFoundError
from app.marketplaces.ozon.service import CRAWL, SEARCH
from app.worker.consumer import SESSION_BY_STREAM, TaskConsumer
from app.worker.handlers import _error_code, _failure_kind
from app.worker.redis_config import redis_worker_settings as rws

DAY = 86400.0
POLICY = SimpleNamespace(load_failure_threshold=3, ban_pause_start_s=600.0,
                         ban_pause_max_s=DAY, ban_level_reset_s=DAY)


class FakeService:
    def __init__(self):
        self.generations = []
        self.resets = []

    def set_device_generation(self, generation, session=None):
        self.generations.append((generation, session))

    def reset_profile(self, session=None):
        self.resets.append(session)


class FakeRedis:
    """BRPOP по списку ключей: запоминает ключи вызова, отдаёт из первого непустого."""

    def __init__(self, queues):
        self.queues = queues
        self.calls = []

    def brpop(self, keys, timeout=0):
        self.calls.append(list(keys))
        for key in keys:
            if self.queues.get(key):
                return key, self.queues[key].pop()
        return None


def make_consumer():
    consumer = TaskConsumer.__new__(TaskConsumer)
    consumer._policy = POLICY
    consumer._running = True
    consumer._load_failures = 0
    consumer._banned_until = 0.0
    consumer._ban_level = 0
    consumer._probe_pending = False
    consumer._generations = {CRAWL: 0, SEARCH: 0}
    consumer.service = FakeService()
    return consumer


def test_failure_kind_and_code():
    assert _failure_kind(PageLoadError("x")) == "load"
    assert _failure_kind(AntibotBlockedError("x")) == "ban"
    assert _failure_kind(ProductNotFoundError("x")) is None
    assert _error_code(PageLoadError("x")) == "antibot_blocked"


def test_stream_to_session():
    assert SESSION_BY_STREAM == {"parse": CRAWL, "search": SEARCH, "select": SEARCH}


def test_ban_registered_immediately():
    consumer = make_consumer()
    bans = []
    consumer._register_ban = lambda reason, session: bans.append(session)
    consumer._track_failure("ban", SEARCH)
    assert bans == [SEARCH]


def test_load_failures_threshold_and_reset():
    consumer = make_consumer()
    bans = []
    consumer._register_ban = lambda reason, session: bans.append(session)
    consumer._track_failure("load", CRAWL)
    consumer._track_failure("load", CRAWL)
    consumer._track_failure(None, CRAWL)        # страница загрузилась — серия обнулена
    consumer._track_failure("load", CRAWL)
    consumer._track_failure("load", CRAWL)
    assert bans == []
    consumer._track_failure("load", CRAWL)
    assert bans == [CRAWL]


def test_register_ban_levels_and_pause():
    consumer = make_consumer()
    start = time.time()
    consumer._register_ban("капча", SEARCH)
    assert consumer._ban_level == 1
    assert 590 <= consumer._banned_until - start <= 610
    consumer._register_ban("капча", SEARCH)
    assert consumer._ban_level == 2
    assert 1190 <= consumer._banned_until - start <= 1210
    assert consumer.service.generations == [(1, SEARCH), (2, SEARCH)]
    assert consumer.service.resets == [SEARCH, SEARCH]


def test_ban_changes_only_own_session():
    consumer = make_consumer()
    consumer._register_ban("капча", SEARCH)
    consumer._register_ban("капча", CRAWL)
    assert consumer._generations == {CRAWL: 1, SEARCH: 1}
    assert consumer.service.generations == [(1, SEARCH), (1, CRAWL)]
    assert consumer.service.resets == [SEARCH, CRAWL]


def test_level_resets_after_quiet_day():
    consumer = make_consumer()
    consumer._ban_level = 5
    consumer._banned_until = time.time() - DAY - 1   # пауза кончилась больше суток назад
    consumer._register_ban("капча", CRAWL)
    assert consumer._ban_level == 1


def _one_iteration(consumer):
    """Один шаг цикла: первая взятая задача останавливает воркер."""
    taken = []

    def process(key, raw):
        taken.append(key)
        consumer._running = False

    consumer._process_one = process
    consumer._consume_loop()
    return taken


def test_probe_task_taken_from_parse_queue():
    search_key, parse_key = rws.search_tasks_key("ozon"), rws.parse_tasks_key("ozon")
    consumer = make_consumer()
    consumer.redis = FakeRedis({search_key: ["{}"], parse_key: ["{}"]})
    consumer._probe_pending = True
    assert _one_iteration(consumer) == [parse_key]
    assert consumer.redis.calls == [rws.parse_task_keys]
    assert consumer._probe_pending is False


def test_regular_task_follows_priority():
    search_key, parse_key = rws.search_tasks_key("ozon"), rws.parse_tasks_key("ozon")
    consumer = make_consumer()
    consumer.redis = FakeRedis({search_key: ["{}"], parse_key: ["{}"]})
    assert _one_iteration(consumer) == [search_key]
    assert consumer.redis.calls == [rws.task_keys]


def test_pause_end_makes_next_task_probe():
    consumer = make_consumer()
    consumer._banned_until = time.time() + 0.05
    assert consumer._wait_ban_pause() is True
    assert consumer._probe_pending is True


def test_parse_task_keys_only_parse():
    assert rws.parse_task_keys == [rws.parse_tasks_key(m) for m in rws.marketplaces]
    assert all(k.startswith("parse:") for k in rws.parse_task_keys)