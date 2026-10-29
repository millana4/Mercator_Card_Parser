# -*- coding: utf-8 -*-
"""Учёт блокировок воркером: две полосы пауз (обход; выдача и подбор), бан сразу,
серия сбоев загрузки, уровень и пауза полосы в памяти процесса, сброс профиля
и устройства у сессии потока, пробная задача после паузы обхода."""
import time
from types import SimpleNamespace

from app.core.exceptions import AntibotBlockedError, PageLoadError, ProductNotFoundError
from app.marketplaces.ozon.service import CRAWL, SEARCH
from app.worker.consumer import LANE_BY_STREAM, SESSION_BY_STREAM, LaneState, TaskConsumer
from app.worker.handlers import _error_code, _failure_kind
from app.worker.redis_config import redis_worker_settings as rws

HOUR = 3600.0
POLICY = SimpleNamespace(load_failure_threshold=3, ban_pause_start_s=600.0,
                         ban_pause_max_s=2 * HOUR, ban_level_reset_s=6 * HOUR)


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
    consumer._lanes = {CRAWL: LaneState(), SEARCH: LaneState()}
    consumer._search_paused = False
    consumer._probe_pending = False
    consumer._generations = {CRAWL: 0, SEARCH: 0}
    consumer.service = FakeService()
    return consumer


def test_failure_kind_and_code():
    assert _failure_kind(PageLoadError("x")) == "load"
    assert _failure_kind(AntibotBlockedError("x")) == "ban"
    assert _failure_kind(ProductNotFoundError("x")) is None
    assert _error_code(PageLoadError("x")) == "antibot_blocked"


def test_lanes_and_sessions_by_stream():
    assert LANE_BY_STREAM == {"parse": CRAWL, "search": SEARCH, "select": SEARCH}
    assert SESSION_BY_STREAM == {"parse": CRAWL, "search": SEARCH, "select": None}


def test_ban_registered_immediately_in_stream_lane():
    consumer = make_consumer()
    bans = []
    consumer._register_ban = lambda lane, stream, reason, session=None: bans.append((lane, stream))
    consumer._track_failure("ban", "select")
    assert bans == [(SEARCH, "select")]


def test_load_failures_counted_per_lane():
    consumer = make_consumer()
    bans = []
    consumer._register_ban = lambda lane, stream, reason, session=None: bans.append(lane)
    consumer._track_failure("load", "parse")
    consumer._track_failure("load", "parse")
    consumer._track_failure("load", "search")   # своя полоса — серия обхода не растёт
    consumer._track_failure(None, "parse")      # страница загрузилась — серия обхода обнулена
    consumer._track_failure("load", "parse")
    consumer._track_failure("load", "parse")
    assert bans == []
    consumer._track_failure("load", "parse")
    assert bans == [CRAWL]


def test_search_ban_keeps_crawl_running():
    consumer = make_consumer()
    start = time.time()
    consumer._register_ban(SEARCH, "search", "капча")
    assert consumer._lanes[CRAWL].until == 0.0
    assert 590 <= consumer._lanes[SEARCH].until - start <= 610
    assert consumer._wait_ban_pause() is False          # обход не на паузе
    assert consumer.service.resets == [SEARCH]
    assert consumer.service.generations == [(1, SEARCH)]


def test_select_ban_resets_nothing():
    consumer = make_consumer()
    consumer._register_ban(SEARCH, "select", "капча")
    assert consumer._lanes[SEARCH].level == 1
    assert consumer.service.resets == [] and consumer.service.generations == []


def test_crawl_ban_pauses_worker_and_resets_crawl():
    consumer = make_consumer()
    consumer._register_ban(CRAWL, "parse", "капча")
    assert consumer._lanes[CRAWL].until > time.time()
    assert consumer._lanes[SEARCH].until == 0.0
    assert consumer.service.resets == [CRAWL]


def test_levels_independent_and_capped():
    consumer = make_consumer()
    for _ in range(6):
        consumer._register_ban(SEARCH, "select", "капча")
    assert consumer._lanes[SEARCH].level == 6
    assert consumer._lanes[SEARCH].until - time.time() <= 2 * HOUR + 1   # потолок 2 ч
    assert consumer._lanes[CRAWL].level == 0


def test_level_resets_after_quiet_period():
    consumer = make_consumer()
    consumer._lanes[SEARCH].level = 5
    consumer._lanes[SEARCH].until = time.time() - 6 * HOUR - 1   # 6 ч без бана после паузы
    consumer._register_ban(SEARCH, "select", "капча")
    assert consumer._lanes[SEARCH].level == 1


def _one_iteration(consumer):
    """Один шаг цикла: первая взятая задача останавливает воркер."""
    taken = []

    def process(key, raw):
        taken.append(key)
        consumer._running = False

    consumer._process_one = process
    consumer._consume_loop()
    return taken


def _queues():
    return {rws.select_tasks_key("ozon"): ["{}"], rws.search_tasks_key("ozon"): ["{}"],
            rws.parse_tasks_key("ozon"): ["{}"]}


def test_search_lane_paused_takes_only_parse():
    consumer = make_consumer()
    consumer.redis = FakeRedis(_queues())
    consumer._register_ban(SEARCH, "select", "капча")
    assert _one_iteration(consumer) == [rws.parse_tasks_key("ozon")]
    assert consumer.redis.calls == [rws.parse_task_keys]


def test_search_lane_pause_end_restores_priority():
    consumer = make_consumer()
    consumer.redis = FakeRedis(_queues())
    consumer._lanes[SEARCH].until = time.time() - 1
    consumer._search_paused = True
    assert _one_iteration(consumer) == [rws.select_tasks_key("ozon")]
    assert consumer._search_paused is False


def test_probe_task_after_crawl_pause_from_parse_queue():
    consumer = make_consumer()
    consumer.redis = FakeRedis(_queues())
    consumer._probe_pending = True
    assert _one_iteration(consumer) == [rws.parse_tasks_key("ozon")]
    assert consumer._probe_pending is False


def test_crawl_pause_end_makes_next_task_probe():
    consumer = make_consumer()
    consumer._lanes[CRAWL].until = time.time() + 0.05
    assert consumer._wait_ban_pause() is True
    assert consumer._probe_pending is True




def test_ban_passes_session_of_blocked_browser():
    """Капча на плитках подбора: в регистрацию бана уходит сессия выдачи (смена
    устройства и профиля выдачи); без пометки - сессия не задана."""
    consumer = make_consumer()
    sessions = []
    consumer._register_ban = lambda lane, stream, reason, session=None: sessions.append(session)
    consumer._track_failure("ban", "select", "search")
    consumer._track_failure("ban", "select")
    assert sessions == ["search", None]