# -*- coding: utf-8 -*-
"""
consumer.py — цикл потребления задач из Redis.

Воркер тянет задачи из общего пула (BRPOP по ключам select/search/parse тех
площадок, что перечислены в PARSER_MARKETPLACES), выполняет и кладёт
результат в список результатов той же площадки. Общий пул = work-stealing:
любой свободный воркер берёт следующую задачу, воркер на паузе после бана
задачи не тянет — их разбирают остальные.

Все задачи идут в одной долгоживущей сессии браузера. Реакция на блокировку:
  - капча/блок (ban) — сразу бан;
  - сбой загрузки (load) — браузер пересоздаётся; load_failure_threshold
    сбоев подряд — бан;
  - бан: сброс профиля браузера и пауза ban_pause_start_s × 2^(уровень−1),
    не больше ban_pause_max_s; уровень обнуляется после ban_level_reset_s
    без бана. Состояние в Redis — перезапуск воркера паузу не обнуляет.
    Первая задача после паузы — пробная.

Подключение к Redis отказоустойчивое: ждёт Redis и переживает его падения.
"""
import json
import time
import signal
import random

import redis

from app.core.logging import get_logger
from app.marketplaces.ozon.service import OzonService
from app.worker.ban_policy import ban_pause_s, next_ban_level
from app.worker.redis_config import redis_worker_settings as rws
from app.worker.result_producer import ResultProducer
from app.worker.handlers import handle_select, handle_parse, handle_search

logger = get_logger(__name__)


class TaskConsumer:
    def __init__(self):
        # Одна долгоживущая сессия браузера и один профиль на все задачи:
        # подбор, выдача, обход.
        self.service = OzonService(use_session=True)
        self.producer = ResultProducer()
        self.redis = None               # подключим в run(), с повторами
        self._running = True
        self._load_failures = 0         # сбоев загрузки подряд
        self._banned_until = 0.0        # конец текущей паузы после бана (epoch, сек)
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
                self._load_ban_state()
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
        """Внутренний цикл: BRPOP по всем ключам задач, пока нас не остановили."""
        while self._running:
            if self.redis is None:
                return
            if self._wait_ban_pause():
                continue  # пауза после бана отсижена — проверяем флаг и берём пробную задачу
            # BRPOP блокируется до задачи или таймаута; таймаут → проверяем _running.
            # Список ключей: Redis отдаёт из первого непустого (приоритет select).
            popped = self.redis.brpop(rws.task_keys, timeout=rws.brpop_timeout_s)
            if popped is None:
                continue  # таймаут — задач нет, проверяем флаг и ждём дальше
            key, raw = popped
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

        # Все потоки идут в одной сессии браузера: блокировка в любом — блокировка IP.
        self._track_failure(failure, marketplace)
        if time.time() < self._banned_until:
            return  # пауза после бана — в начале следующей итерации цикла
        self._pause_between_cards()

    # ------------------------------------------------------------------ #
    def _track_failure(self, failure: str | None, marketplace: str) -> None:
        """Учёт блокировок: ban — сразу бан; load — бан после серии сбоев подряд;
        любой другой исход (страница загрузилась) обнуляет серию сбоев."""
        if failure == "ban":
            self._register_ban(marketplace, "капча или блокировка")
        elif failure == "load":
            self._load_failures += 1
            threshold = self._policy.load_failure_threshold
            logger.warning("Сбой загрузки подряд: %s/%s", self._load_failures, threshold)
            if self._load_failures >= threshold:
                self._register_ban(marketplace, f"сбоев загрузки подряд: {self._load_failures}")
        else:
            self._load_failures = 0

    def _register_ban(self, marketplace: str, reason: str) -> None:
        """Бан: рассчитать уровень и паузу, перейти на следующее устройство
        браузера, записать состояние в Redis, сбросить профиль браузера.
        Сама пауза — в _wait_ban_pause."""
        now = time.time()
        key = rws.ban_state_key(marketplace)
        prev_level, prev_until, prev_generation = 0, 0.0, 0
        try:
            state = self.redis.hgetall(key) if self.redis is not None else {}
            prev_level = int(float(state.get("level", 0)))
            prev_until = float(state.get("until", 0))
            prev_generation = int(float(state.get("generation", 0)))
        except Exception as e:
            logger.warning("Не удалось прочитать состояние бана %s: %s", key, e)

        level = next_ban_level(prev_level, prev_until, now, self._policy.ban_level_reset_s)
        pause = ban_pause_s(level, self._policy.ban_pause_start_s, self._policy.ban_pause_max_s)
        until = now + pause
        generation = prev_generation + 1
        self._banned_until = max(self._banned_until, until)
        self._load_failures = 0

        try:
            if self.redis is not None:
                self.redis.hset(key, mapping={"level": level, "until": until,
                                              "banned_at": now, "reason": reason,
                                              "generation": generation})
        except Exception as e:
            logger.warning("Не удалось записать состояние бана %s: %s", key, e)

        logger.warning("БАН (%s): уровень %s, пауза %.0f мин до %s — новое устройство "
                       "(поколение %s), сбрасываю профиль браузера",
                       reason, level, pause / 60,
                       time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)), generation)
        try:
            self.service.set_device_generation(generation)
            self.service.reset_profile()
        except Exception as e:
            logger.warning("Ошибка смены устройства или сброса профиля браузера: %s", e)

    def _load_ban_state(self) -> None:
        """При подключении к Redis восстановить незакончившуюся паузу после бана
        и поколение устройства браузера."""
        generation = 0
        for marketplace in rws.marketplaces:
            key = rws.ban_state_key(marketplace)
            try:
                state = self.redis.hgetall(key)
                until = float(state.get("until", 0))
                generation = max(generation, int(float(state.get("generation", 0))))
            except Exception as e:
                logger.warning("Не удалось прочитать состояние бана %s: %s", key, e)
                continue
            if until > time.time():
                self._banned_until = max(self._banned_until, until)
                logger.warning("Восстановлена пауза после бана: площадка=%s, уровень=%s, до %s",
                               marketplace, state.get("level"),
                               time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)))
        self.service.set_device_generation(generation)
        logger.info("Поколение устройства браузера: %s", generation)

    def _wait_ban_pause(self) -> bool:
        """Если идёт пауза после бана — отсидеть её (прерываемо). True, если ждали."""
        remaining = self._banned_until - time.time()
        if remaining <= 0:
            return False
        logger.warning("Пауза после бана: %.0f мин — задачи не беру", remaining / 60)
        self._interruptible_sleep(remaining)
        if self._running:
            logger.info("Пауза после бана окончена — следующая задача пробная")
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