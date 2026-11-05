# -*- coding: utf-8 -*-
"""Учёт блокировок воркером: две полосы пауз (обход; выдача и подбор), бан сразу,
серия сбоев загрузки, уровень и пауза полосы в памяти процесса, сброс профиля
и устройства у сессии потока, пробная задача после паузы обхода. Приоритет очередей:
сначала обход. Простой браузера выдачи проверяется после каждой задачи. Серия сбоев
загрузки при недоступном Ozon названа в причине бана блокировкой IP."""
import time
import pytest
from types import SimpleNamespace

import app.worker.consumer as consumer_module
from app.core.exceptions import AntibotBlockedError, PageLoadError, ProductNotFoundError
from app.marketplaces.ozon.service import CRAWL, SEARCH
from app.worker.consumer import LANE_BY_STREAM, SESSION_BY_STREAM, LaneState, TaskConsumer
from app.worker.handlers import _error_code, _failure_kind
from app.worker.redis_config import redis_worker_settings as rws

HOUR = 3600.0
POLICY = SimpleNamespace(load_failure_threshold=3, ban_pause_start_s=600.0,
                         ban_pause_max_s=2 * HOUR, ban_level_reset_s=6 * HOUR,
                         search_idle_close_s=900.0,  crawl_between_select=20)


class FakeService:
    def __init__(self):
        self.generations = []
        self.resets = []
        self.idle_checks = []

    def set_device_generation(self, generation, session=None):
        self.generations.append((generation, session))

    def reset_profile(self, session=None):
        self.resets.append(session)

    def close_search_if_idle(self, idle_s):
        self.idle_checks.append(idle_s)
        return False


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


@pytest.fixture(autouse=True)
def _ozon_reachable_in_tests(monkeypatch):
    """Тесты не ходят в сеть: по умолчанию Ozon «доступен»."""
    monkeypatch.setattr(consumer_module, "_ozon_reachable", lambda timeout_s=10.0: True)


def make_consumer():
    consumer = TaskConsumer.__new__(TaskConsumer)
    consumer._policy = POLICY
    consumer._running = True
    consumer._lanes = {CRAWL: LaneState(), SEARCH: LaneState()}
    consumer._search_paused = False
    consumer._probe_pending = False
    consumer._generations = {CRAWL: 0, SEARCH: 0}
    consumer._task_keys = consumer_module._queue_order(rws.task_keys, rws.key_routes, select_turn=False)
    consumer._turn_keys = consumer_module._queue_order(rws.task_keys, rws.key_routes, select_turn=True)
    consumer._parse_streak = 0
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


def test_queue_order():
    """Обычно обход, подбор, выдача; в ход подбора - подбор, обход, выдача. Выдача всегда последняя."""
    routes = {"sel": ("select", "ozon"), "srch": ("search", "ozon"), "par": ("parse", "ozon")}
    keys = ["sel", "srch", "par"]
    assert consumer_module._queue_order(keys, routes, select_turn=False) == ["par", "sel", "srch"]
    assert consumer_module._queue_order(keys, routes, select_turn=True) == ["sel", "par", "srch"]


def test_crawl_task_taken_before_select_and_search():
    """Все очереди непустые — воркер берёт задачу обхода."""
    consumer = make_consumer()
    consumer.redis = FakeRedis(_queues())
    assert _one_iteration(consumer) == [rws.parse_tasks_key("ozon")]
    assert consumer.redis.calls == [consumer._task_keys]


def test_keys_for_next_task():
    """До порога - обход первым; на пороге - подбор и выдача первыми; 0 - порог выключен."""
    all_keys = ["select", "search", "parse"]
    crawl_first = ["parse", "select", "search"]
    assert consumer_module._keys_for_next_task(0, 20, crawl_first, all_keys) == crawl_first
    assert consumer_module._keys_for_next_task(19, 20, crawl_first, all_keys) == crawl_first
    assert consumer_module._keys_for_next_task(20, 20, crawl_first, all_keys) == all_keys
    assert consumer_module._keys_for_next_task(500, 0, crawl_first, all_keys) == crawl_first


def test_select_turn_after_n_crawl_tasks():
    """Очередь обхода непустая, но после N задач обхода берётся подбор; счётчик обнуляется."""
    consumer = make_consumer()
    consumer._parse_streak = POLICY.crawl_between_select
    consumer.redis = FakeRedis(_queues())
    assert _one_iteration(consumer) == [rws.select_tasks_key("ozon")]
    assert consumer.redis.calls == [consumer._turn_keys]
    assert consumer._parse_streak == 0


def test_select_turn_without_select_tasks_falls_back_to_crawl():
    """Подбора и выдачи нет - берётся обход, и очерёдность подбора не теряется."""
    consumer = make_consumer()
    consumer._parse_streak = POLICY.crawl_between_select
    queues = _queues()
    queues[rws.select_tasks_key("ozon")] = []
    queues[rws.search_tasks_key("ozon")] = []
    consumer.redis = FakeRedis(queues)
    assert _one_iteration(consumer) == [rws.parse_tasks_key("ozon")]
    assert consumer._parse_streak == POLICY.crawl_between_select + 1


def test_crawl_tasks_counted_in_streak():
    consumer = make_consumer()
    consumer.redis = FakeRedis(_queues())
    assert _one_iteration(consumer) == [rws.parse_tasks_key("ozon")]
    assert consumer._parse_streak == 1


def test_search_taken_only_when_crawl_and_select_empty():
    """Выдача последняя: берётся, только когда пусты и обход, и подбор."""
    consumer = make_consumer()
    queues = _queues()
    queues[rws.parse_tasks_key("ozon")] = []
    consumer.redis = FakeRedis(queues)
    assert _one_iteration(consumer) == [rws.select_tasks_key("ozon")]     # подбор раньше выдачи

    consumer = make_consumer()
    queues = _queues()
    queues[rws.parse_tasks_key("ozon")] = []
    queues[rws.select_tasks_key("ozon")] = []
    consumer.redis = FakeRedis(queues)
    assert _one_iteration(consumer) == [rws.search_tasks_key("ozon")]


def test_select_turn_without_select_prefers_crawl_over_search():
    """Ход подбора, а подбора нет: берётся обход, а не выдача."""
    consumer = make_consumer()
    consumer._parse_streak = POLICY.crawl_between_select
    queues = _queues()
    queues[rws.select_tasks_key("ozon")] = []
    consumer.redis = FakeRedis(queues)
    assert _one_iteration(consumer) == [rws.parse_tasks_key("ozon")]


def test_select_resets_streak_even_when_taken_without_turn():
    """Подбор взят при пустом обходе: до следующего хода снова нужно N задач обхода."""
    consumer = make_consumer()
    consumer._parse_streak = 5
    queues = _queues()
    queues[rws.parse_tasks_key("ozon")] = []
    consumer.redis = FakeRedis(queues)
    assert _one_iteration(consumer) == [rws.select_tasks_key("ozon")]
    assert consumer._parse_streak == 0


def test_search_does_not_reset_streak():
    consumer = make_consumer()
    consumer._parse_streak = 5
    queues = _queues()
    queues[rws.parse_tasks_key("ozon")] = []
    queues[rws.select_tasks_key("ozon")] = []
    consumer.redis = FakeRedis(queues)
    assert _one_iteration(consumer) == [rws.search_tasks_key("ozon")]
    assert consumer._parse_streak == 5


def test_select_taken_when_crawl_queue_empty():
    """Очередь обхода пуста — берётся подбор (следующий по приоритету)."""
    consumer = make_consumer()
    queues = _queues()
    queues[rws.parse_tasks_key("ozon")] = []
    consumer.redis = FakeRedis(queues)
    assert _one_iteration(consumer) == [rws.select_tasks_key("ozon")]


def test_search_lane_paused_takes_only_parse():
    consumer = make_consumer()
    consumer.redis = FakeRedis(_queues())
    consumer._register_ban(SEARCH, "select", "капча")
    assert _one_iteration(consumer) == [rws.parse_tasks_key("ozon")]
    assert consumer.redis.calls == [rws.parse_task_keys]


def test_search_lane_pause_end_restores_all_queues():
    """Пауза выдачи и подбора кончилась — снова слушаются все очереди (обход первым)."""
    consumer = make_consumer()
    consumer.redis = FakeRedis(_queues())
    consumer._lanes[SEARCH].until = time.time() - 1
    consumer._search_paused = True
    assert _one_iteration(consumer) == [rws.parse_tasks_key("ozon")]
    assert consumer.redis.calls == [consumer._task_keys]
    assert consumer._search_paused is False


def test_idle_search_checked_after_each_task():
    """После задачи цикл проверяет простой браузера выдачи с порогом из настроек."""
    consumer = make_consumer()
    consumer.redis = FakeRedis(_queues())
    _one_iteration(consumer)
    assert consumer.service.idle_checks == [POLICY.search_idle_close_s]


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


def test_ban_reason_names_ip_block_when_ozon_unreachable(monkeypatch):
    """Серия сбоев загрузки и нет соединения с Ozon — в причине бана прямо: блокировка IP."""
    monkeypatch.setattr(consumer_module, "_ozon_reachable", lambda timeout_s=10.0: False)
    consumer = make_consumer()
    reasons = []
    consumer._register_ban = lambda lane, stream, reason, session=None: reasons.append(reason)
    for _ in range(consumer._policy.load_failure_threshold):
        consumer._track_failure("load", "parse")
    assert len(reasons) == 1 and "Ozon не отвечает этому IP" in reasons[0]


def test_ban_reason_plain_when_ozon_reachable():
    consumer = make_consumer()
    reasons = []
    consumer._register_ban = lambda lane, stream, reason, session=None: reasons.append(reason)
    for _ in range(consumer._policy.load_failure_threshold):
        consumer._track_failure("load", "parse")
    assert len(reasons) == 1 and "не отвечает этому IP" not in reasons[0]