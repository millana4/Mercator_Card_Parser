# -*- coding: utf-8 -*-
"""
consumer.py — цикл потребления задач из Redis.

Воркер тянет задачи из общего пула (BRPOP по ключам select/parse тех
площадок, что перечислены в PARSER_MARKETPLACES), выполняет и кладёт
результат в список результатов той же площадки. Общий пул = work-stealing:
любой свободный воркер берёт следующую задачу, застрявший на антибот-паузе
просто не тянет новую — остальные продолжают.

Подключение к Redis отказоустойчивое: ждёт Redis и переживает его падения.
"""
import json
import time
import signal
import random

import redis

from app.core.logging import get_logger
from app.marketplaces.ozon.service import OzonService
from app.worker.redis_config import redis_worker_settings as rws
from app.worker.result_producer import ResultProducer
from app.worker.handlers import handle_select, handle_parse

logger = get_logger(__name__)


class TaskConsumer:
    def __init__(self):
        self.service = OzonService()
        self.producer = ResultProducer()
        self.redis = None               # подключим в run(), с повторами
        self._running = True
        self._antibot_streak = 0        # блокировок подряд
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
        """Внутренний цикл: BRPOP по всем ключам задач, пока нас не остановили."""
        while self._running:
            if self.redis is None:
                return
            # BRPOP блокируется до задачи или таймаута; таймаут → проверяем _running.
            # Список ключей: Redis отдаёт из первого непустого (приоритет select).
            popped = self.redis.brpop(rws.task_keys, timeout=rws.brpop_timeout_s)
            if popped is None:
                continue  # таймаут — задач нет, проверяем флаг и ждём дальше
            key, raw = popped
            self._process_one(key, raw)
            if self.redis is None:
                return  # cooldown обнулил клиент внутри обработки

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
        else:
            result = handle_parse(task, self.service)
            result_key = rws.parse_results_key(marketplace)

        # Проставляем, какой воркер отдал результат (наблюдаемость).
        result["parser_id"] = rws.parser_id

        try:
            self.producer.send(result_key, result)
        except Exception:
            # Результат не ушёл — задача уже вынута из очереди (BRPOP удалил).
            # Ядро переотправит карточку по истечении inflight-timeout. Не падаем.
            logger.error("Результат не отправлен | task_id=%s — ядро переотправит по таймауту",
                         result.get("task_id"))
            return

        # Счётчики наблюдаемости — свои на каждую площадку.
        if result.get("error_code") == "antibot_blocked":
            self.producer.incr_counter(rws.counter_antibot(marketplace))
        else:
            self.producer.incr_counter(rws.counter_done(marketplace))

        # Реакция на антибот — только для задач обхода.
        if stream == "parse":
            self._react_to_antibot(result)

        self._pause_between_cards()

    def _react_to_antibot(self, result: dict) -> None:
        """Считает блокировки подряд. При полосе — cooldown: воркер перестаёт
        тянуть задачи на время остывания IP. Задачи лежат в общем пуле,
        их разберут другие воркеры."""
        if result.get("error_code") == "antibot_blocked":
            self._antibot_streak += 1
            logger.warning("Антибот подряд: %s/%s",
                           self._antibot_streak,
                           self._policy.antibot_streak_threshold)
            if self._antibot_streak >= self._policy.antibot_streak_threshold:
                self._antibot_streak = 0
                self._cooldown()
        else:
            self._antibot_streak = 0

    def _cooldown(self) -> None:
        """Полоса блокировок: закрыть браузер-сессию (IP забанен), отсидеть
        cooldown. Пока этот воркер спит, задачи из общего пула разбирают другие."""
        cooldown = self._policy.antibot_cooldown_s
        logger.warning("Полоса блокировок — cooldown %s сек (задачи разберут другие воркеры)",
                       cooldown)
        try:
            self.service.close_crawl()
        except Exception as e:
            logger.debug("Ошибка закрытия сессии при cooldown: %s", e)
        self._interruptible_sleep(cooldown)
        logger.info("Cooldown окончен — продолжаю тянуть задачи")

    def _pause_between_cards(self) -> None:
        """Пауза между карточками. Для reserve — щадящая, для primary — обычная."""
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