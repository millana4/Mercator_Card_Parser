# -*- coding: utf-8 -*-
"""
consumer.py — цикл потребления задач из Redis.

Воркер тянет задачи из общего пула (BRPOP по ключам select/search/parse тех
площадок, что перечислены в PARSER_MARKETPLACES), выполняет и кладёт
результат в список результатов той же площадки. Общий пул = work-stealing:
любой свободный воркер берёт следующую задачу, воркер на паузе после бана
задачи не тянет — их разбирают остальные.

Браузеры (см. OzonService): обход — в долгоживущей сессии обхода, снимки
выдачи — в долгоживущей сессии выдачи, подбор — новым браузером на каждую страницу.

Две независимые полосы с паузами после бана:
  - полоса обхода (parse): бан останавливает весь воркер — IP помечен на карточках;
  - полоса выдачи и подбора (search, select): бан останавливает только выдачу
    и подбор, обход продолжается — воркер слушает только очередь обхода.
Реакция на блокировку в полосе:
  - капча/блок (ban) — сразу бан;
  - сбой загрузки (load) — браузер пересоздаётся; load_failure_threshold
    сбоев подряд в полосе — бан;
  - бан: пауза ban_pause_start_s × 2^(уровень−1), не больше ban_pause_max_s;
    уровень полосы обнуляется после ban_level_reset_s без бана. У выдачи и обхода
    сбрасываются профиль и устройство своей сессии; у подбора сбрасывать нечего.
  - после паузы обхода первая задача — пробная, только из очереди обхода.
Устаревшие задачи выдачи (expired) не идут на Ozon: без счётчиков, учёта и паузы.
Уровни, паузы и поколения устройств хранятся только в памяти процесса:
перезапуск воркера их обнуляет (баны редкие, решаются ротацией IP).

Подключение к Redis отказоустойчивое: ждёт Redis и переживает его падения.
"""
import json
import time
import signal
import random
from dataclasses import dataclass

import redis

from app.core.logging import get_logger
from app.marketplaces.ozon.service import CRAWL, SEARCH, OzonService
from app.worker.ban_policy import ban_pause_s, next_ban_level
from app.worker.redis_config import redis_worker_settings as rws
from app.worker.result_producer import ResultProducer
from app.worker.handlers import handle_select, handle_parse, handle_search

logger = get_logger(__name__)

# Поток задачи -> полоса пауз: обход отдельно, выдача и подбор вместе.
LANE_BY_STREAM = {"parse": CRAWL, "search": SEARCH, "select": SEARCH}
# Поток задачи -> долгоживущая сессия, у которой при бане сбрасываются профиль и
# устройство. Подбор идёт новыми браузерами с временным профилем — сбрасывать нечего.
SESSION_BY_STREAM = {"parse": CRAWL, "search": SEARCH, "select": None}
LANE_TITLE = {CRAWL: "обход", SEARCH: "выдача и подбор"}


@dataclass
class LaneState:
    """Состояние полосы: уровень бана, конец паузы, сбоев загрузки подряд."""
    level: int = 0
    until: float = 0.0
    load_failures: int = 0


class TaskConsumer:
    def __init__(self):
        # Две долгоживущие сессии браузера: обход и выдача; подбор — новыми браузерами.
        self.service = OzonService(use_session=True)
        self.producer = ResultProducer()
        self.redis = None               # подключим в run(), с повторами
        self._running = True
        self._lanes = {CRAWL: LaneState(), SEARCH: LaneState()}
        self._search_paused = False     # выдача и подбор на паузе — слушаем только обход
        self._probe_pending = False     # следующая задача — пробная, только из очереди обхода
        # Поколение устройства по сессиям: растёт при бане своей сессии.
        self._generations = {CRAWL: 0, SEARCH: 0}
        self._policy = self.service.policy
        # Ключ задачи -> (поток, маркетплейс). Считаем один раз при старте.
        self._routes = rws.key_routes

    def stop(self, *_):
        logger.info("Сигнал остановки — завершаю после текущей задачи")
        self._running = False

    def _connect_redis(self) -> redis.Redis:
        """Подключить клиент Redis, повторяя попытки, пока он не станет доступен."""
        attempt = 0
        while self._running:
            attempt += 1
            try:
                client = redis.Redis(
                    host=rws.host, port=rws.port, db=rws.db,
                    password=rws.password,
                    decode_responses=True,
                    socket_connect_timeout=10,
                )
                client.ping()
                logger.info("TaskConsumer подключён к Redis %s:%s | ключи=%s (попытка %s)",
                            rws.host, rws.port, rws.task_keys, attempt)
                return client
            except Exception as e:
                logger.warning("Redis недоступен (%s:%s): %s — жду, попытка %s",
                               rws.host, rws.port, e, attempt)
                time.sleep(rws.reconnect_delay_s)
        return None

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        logger.info("Воркер запущен | регион=%s (%s) | площадки=%s | роль=%s | parser_id=%s",
                    rws.geo_code, rws.geo_name, ", ".join(rws.marketplaces),
                    rws.role, rws.parser_id)

        while self._running:
            if self.redis is None:
                self.redis = self._connect_redis()
                if self.redis is None:
                    break  # остановили во время ожидания Redis
            try:
                self._consume_loop()
            except redis.exceptions.RedisError as e:
                logger.warning("Потеряна связь с Redis: %s — переподключаюсь", e)
                self.redis = None
                time.sleep(rws.reconnect_delay_s)
            except Exception as e:
                logger.exception("Непредвиденная ошибка в цикле потребления: %s", e)
                self.redis = None
                time.sleep(rws.reconnect_delay_s)

        self._shutdown()

    def _consume_loop(self):
        """Внутренний цикл: BRPOP по ключам задач, пока нас не остановили."""
        while self._running:
            if self.redis is None:
                return
            if self._wait_ban_pause():
                continue  # пауза обхода отсижена — проверяем флаг и берём пробную задачу
            # BRPOP блокируется до задачи или таймаута; таймаут → проверяем _running.
            # Список ключей: Redis отдаёт из первого непустого (приоритет select).
            # Только очередь обхода: пробная задача после паузы обхода или
            # выдача и подбор на паузе.
            parse_only = self._probe_pending or self._search_lane_paused()
            keys = rws.parse_task_keys if parse_only else rws.task_keys
            popped = self.redis.brpop(keys, timeout=rws.brpop_timeout_s)
            if popped is None:
                continue  # таймаут — задач нет, проверяем флаг и ждём дальше
            key, raw = popped
            self._probe_pending = False  # пробная задача взята; новый бан снова её назначит
            self._process_one(key, raw)

    def _process_one(self, key: str, raw: str) -> None:
        route = self._routes.get(key)
        if route is None:
            logger.warning("Задача из неожиданного ключа %s — пропускаю", key)
            return
        stream, marketplace = route

        try:
            task = json.loads(raw)
        except Exception as e:
            logger.error("Не разобрать задачу из %s: %s — пропускаю", key, e)
            return

        logger.info("Получена задача из %s (площадка=%s, поток=%s)", key, marketplace, stream)

        if stream == "select":
            result = handle_select(task, self.service)
            result_key = rws.select_results_key(marketplace)
        elif stream == "search":
            result = handle_search(task, self.service)
            result_key = rws.search_results_key(marketplace)
        else:
            result = handle_parse(task, self.service)
            result_key = rws.parse_results_key(marketplace)

        # Внутренний признак блокировки/сбоя — ядру не отправляем.
        failure = result.pop("_failure", None)
        # Проставляем, какой воркер отдал результат (наблюдаемость).
        result["parser_id"] = rws.parser_id

        try:
            self.producer.send(result_key, result)
        except Exception:
            # Результат не ушёл — задача уже вынута из очереди (BRPOP удалил).
            # Ядро переотправит по истечении inflight-timeout. Учёт и пауза — как обычно.
            logger.error("Результат не отправлен | task_id=%s — ядро переотправит по таймауту",
                         result.get("task_id"))

        # Устаревшая задача выдачи: на Ozon не ходили — без счётчиков, учёта и паузы.
        if failure == "expired":
            return

        # Счётчики наблюдаемости — свои на каждую площадку; у снимков выдачи — отдельные.
        if failure == "load":
            self.producer.incr_counter(rws.counter_load_failed(marketplace))
        elif stream == "search":
            self.producer.incr_counter(rws.counter_search_antibot(marketplace) if failure == "ban"
                                       else rws.counter_search_done(marketplace))
        elif failure == "ban":
            self.producer.incr_counter(rws.counter_antibot(marketplace))
        else:
            self.producer.incr_counter(rws.counter_done(marketplace))

        # Пауза — в полосе задачи; профиль и устройство — у сессии задачи.
        self._track_failure(failure, stream)
        if time.time() < self._lanes[CRAWL].until:
            return  # пауза обхода — в начале следующей итерации цикла
        self._pause_between_cards()

    # ------------------------------------------------------------------ #
    def _track_failure(self, failure: str | None, stream: str) -> None:
        """Учёт блокировок в полосе потока: ban — сразу бан; load — бан после серии
        сбоев подряд; любой другой исход (страница загрузилась) обнуляет серию."""
        lane = LANE_BY_STREAM.get(stream, CRAWL)
        state = self._lanes[lane]
        if failure == "ban":
            self._register_ban(lane, stream, "капча или блокировка")
        elif failure == "load":
            state.load_failures += 1
            threshold = self._policy.load_failure_threshold
            logger.warning("Сбой загрузки подряд (%s): %s/%s",
                           LANE_TITLE[lane], state.load_failures, threshold)
            if state.load_failures >= threshold:
                self._register_ban(lane, stream, f"сбоев загрузки подряд: {state.load_failures}")
        else:
            state.load_failures = 0

    def _register_ban(self, lane: str, stream: str, reason: str) -> None:
        """Бан в полосе: уровень и пауза полосы; у сессии потока — новое устройство
        и сброс профиля. Уровень растёт, если с конца прошлой паузы полосы прошло
        меньше ban_level_reset_s, иначе снова 1."""
        state = self._lanes[lane]
        now = time.time()
        level = next_ban_level(state.level, state.until, now, self._policy.ban_level_reset_s)
        pause = ban_pause_s(level, self._policy.ban_pause_start_s, self._policy.ban_pause_max_s)
        state.level = level
        state.until = now + pause
        state.load_failures = 0
        until_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.until))
        if lane == CRAWL:
            logger.warning("БАН обхода (%s, поток %s): уровень %s, пауза %.0f мин до %s — "
                           "весь воркер на паузе", reason, stream, level, pause / 60, until_text)
        else:
            self._search_paused = True
            logger.warning("БАН выдачи и подбора (%s, поток %s): уровень %s, пауза %.0f мин до %s — "
                           "обход продолжается", reason, stream, level, pause / 60, until_text)

        session = SESSION_BY_STREAM.get(stream)
        if session is None:
            return  # подбор: новые браузеры с временным профилем — сбрасывать нечего
        self._generations[session] += 1
        logger.warning("Новое устройство сессии %s (поколение %s), сбрасываю её профиль",
                       session, self._generations[session])
        try:
            self.service.set_device_generation(self._generations[session], session)
            self.service.reset_profile(session)
        except Exception as e:
            logger.warning("Ошибка смены устройства или сброса профиля браузера: %s", e)

    def _search_lane_paused(self) -> bool:
        """Выдача и подбор на паузе? При окончании паузы — одна запись в лог."""
        if time.time() < self._lanes[SEARCH].until:
            return True
        if self._search_paused:
            self._search_paused = False
            logger.info("Пауза выдачи и подбора окончена — снова беру все задачи")
        return False

    def _wait_ban_pause(self) -> bool:
        """Если идёт пауза обхода — отсидеть её (прерываемо). True, если ждали."""
        remaining = self._lanes[CRAWL].until - time.time()
        if remaining <= 0:
            return False
        logger.warning("Пауза после бана обхода: %.0f мин — задачи не беру", remaining / 60)
        self._interruptible_sleep(remaining)
        if self._running:
            self._probe_pending = True
            logger.info("Пауза обхода окончена — следующая задача пробная (из очереди обхода)")
        return True

    def _pause_between_cards(self) -> None:
        """Пауза между задачами. Для reserve — щадящая, для primary — обычная."""
        if rws.role == "reserve":
            lo, hi = (self._policy.reserve_between_cards_min,
                      self._policy.reserve_between_cards_max)
        else:
            lo, hi = (self._policy.between_cards_min,
                      self._policy.between_cards_max)
        delay = random.uniform(lo, hi)
        self._interruptible_sleep(delay)

    def _interruptible_sleep(self, seconds: float) -> None:
        """Спит, но просыпается на остановку воркера."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(2.0, end - time.time()))

    def _shutdown(self):
        logger.info("Останавливаю воркер...")
        try:
            self.service.close_crawl()
        except Exception as e:
            logger.debug("Ошибка закрытия сессии обхода: %s", e)
        self.producer.close()
        logger.info("Воркер остановлен")