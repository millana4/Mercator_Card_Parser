# -*- coding: utf-8 -*-
"""
consumer.py — цикл потребления задач.
Читает задачи своего региона по одной, маршрутизирует по имени топика,
отправляет результат и ТОЛЬКО ПОСЛЕ этого коммитит смещение.
Подключение к брокеру отказоустойчивое: ждёт брокер и переживает его падения.
"""

import json
import time
import signal
import random
from kafka import KafkaConsumer, TopicPartition, OffsetAndMetadata
from kafka.errors import NoBrokersAvailable, KafkaError

from app.core.logging import get_logger
from app.marketplaces.ozon.service import OzonService
from app.worker.kafka_config import kafka_worker_settings as kws
from app.worker.result_producer import ResultProducer
from app.worker.handlers import handle_select, handle_parse

logger = get_logger(__name__)


class TaskConsumer:
    def __init__(self):
        # сервис и продюсер создаём сразу; продюсер сам ждёт брокер (см. ResultProducer)
        self.service = OzonService()
        self.producer = ResultProducer()
        self.consumer = None            # подключим в run(), с повторами
        self._running = True
        self._antibot_streak = 0  # блокировок подряд
        self._policy = self.service.policy  # политика с параметрами пауз

    def stop(self, *_):
        logger.info("Сигнал остановки — завершаю после текущей задачи")
        self._running = False

    def _connect_consumer(self) -> KafkaConsumer:
        """Подключить консьюмер, повторяя попытки, пока брокер не станет доступен."""
        attempt = 0
        while self._running:
            attempt += 1
            try:
                consumer = KafkaConsumer(
                    *kws.task_topics,
                    bootstrap_servers=kws.bootstrap_servers,
                    group_id=kws.group_id,
                    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                    enable_auto_commit=False,
                    max_poll_records=1,
                    auto_offset_reset="earliest",
                    max_poll_interval_ms=1800000,
                    session_timeout_ms=60000,
                    heartbeat_interval_ms=20000,
                    **kws.kafka_auth_kwargs,
                )
                logger.info("TaskConsumer подписан на %s | group=%s | brokers=%s (попытка %s)",
                            kws.task_topics, kws.group_id, kws.bootstrap_servers, attempt)
                return consumer
            except NoBrokersAvailable:
                logger.warning("Брокер недоступен (%s), жду... попытка %s",
                               kws.bootstrap_servers, attempt)
                time.sleep(kws.reconnect_delay_s)
            except Exception as e:
                logger.warning("Не удалось подключить консьюмер: %s — жду, попытка %s",
                               e, attempt)
                time.sleep(kws.reconnect_delay_s)
        return None  # остановили до подключения

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        logger.info("Воркер запущен, регион=%s (%s)", kws.geo_code, kws.geo_name)

        # внешний цикл: при падении брокера переподключаемся и продолжаем
        while self._running:
            if self.consumer is None:
                self.consumer = self._connect_consumer()
                if self.consumer is None:
                    break  # остановили во время ожидания брокера
            try:
                self._consume_loop()
            except (NoBrokersAvailable, KafkaError) as e:
                # брокер отвалился в процессе — закрываем и переподключаемся
                logger.warning("Потеряна связь с брокером: %s — переподключаюсь", e)
                self._close_consumer()
                self.consumer = None
                time.sleep(kws.reconnect_delay_s)
            except Exception as e:
                logger.exception("Непредвиденная ошибка в цикле потребления: %s", e)
                self._close_consumer()
                self.consumer = None
                time.sleep(kws.reconnect_delay_s)

        self._shutdown()

    def _consume_loop(self):
        """Внутренний цикл: пока связь есть и нас не остановили."""
        while self._running:
            if self.consumer is None:
                # consumer закрыт (например, cooldown-выход из группы) —
                # выходим во внешний цикл run(), он переподключит.
                return
            batch = self.consumer.poll(timeout_ms=kws.poll_timeout_ms)
            if not batch:
                continue
            for tp, messages in batch.items():
                for message in messages:
                    if not self._running:
                        return
                    self._process_one(tp.topic, message)
                    if self.consumer is None:
                        # cooldown-выход произошёл внутри обработки — выходим.
                        return

    def _process_one(self, topic: str, message) -> None:
        task = message.value
        logger.info("Получена задача из %s (offset=%s)", topic, message.offset)

        if topic == kws.select_tasks_topic:
            result = handle_select(task, self.service)
            result_topic = kws.select_results_topic
        elif topic == kws.parse_tasks_topic:
            result = handle_parse(task, self.service)
            result_topic = kws.parse_results_topic
        else:
            logger.warning("Задача из неожиданного топика %s — пропускаю", topic)
            self._commit(message)
            return

        try:
            self.producer.send(result_topic, result)
        except Exception:
            logger.error("Результат не отправлен — смещение НЕ коммичу, задача перечитается")
            return
        self._commit(message)

        # Реакция на антибот: считаем блокировки подряд, при полосе — cooldown.
        if topic == kws.parse_tasks_topic:
            self._react_to_antibot(result)
        # Небольшая пауза между карточками.
        self._pause_between_cards()

    def _react_to_antibot(self, result: dict) -> None:
        """Считает блокировки подряд. При полосе — воркер ВЫХОДИТ из группы на
        время cooldown: закрывает consumer, чтобы Kafka отдала его партицию
        живым парсерам (rebalance), спит, затем внешний цикл run() переподключит.
        Так забаненный IP не держит партицию — карточки уходят живым."""
        if result.get("error_code") == "antibot_blocked":
            self._antibot_streak += 1
            logger.warning("Антибот подряд: %s/%s",
                           self._antibot_streak,
                           self._policy.antibot_streak_threshold)
            if self._antibot_streak >= self._policy.antibot_streak_threshold:
                self._antibot_streak = 0
                self._cooldown_release_partition()
        else:
            # любой не-антибот результат (успех/др. ошибка) сбрасывает серию
            self._antibot_streak = 0

    def _cooldown_release_partition(self) -> None:
        """Выйти из группы (освободить партицию живым), поспать cooldown,
        закрыть браузер сессии, затем пометить consumer как отсутствующий —
        внешний цикл run() переподключит воркер обратно в группу."""
        cooldown = self._policy.antibot_cooldown_s
        logger.warning("Полоса блокировок — выхожу из группы, партиция уйдёт "
                       "живым парсерам; cooldown %s сек", cooldown)
        # 1. Закрыть consumer → Kafka сделает rebalance, партиция уйдёт живым.
        self._close_consumer()
        self.consumer = None
        # 2. Закрыть браузер сессии (IP забанен, сессия всё равно мертва).
        try:
            self.service.close_crawl()
        except Exception as e:
            logger.debug("Ошибка закрытия сессии при cooldown: %s", e)
        # 3. Отсидеть cooldown (IP остывает), прерываемо на остановку воркера.
        self._interruptible_sleep(cooldown)
        logger.info("Cooldown окончен — переподключаюсь к группе")
        # consumer=None → внешний цикл run() вызовет _connect_consumer заново.

    def _pause_between_cards(self) -> None:
        """Пауза между карточками. Для reserve-парсера — щадящая (~1 обход/сутки),
        для primary — обычная."""
        if kws.role == "reserve":
            lo, hi = (self._policy.reserve_between_cards_min,
                      self._policy.reserve_between_cards_max)
        else:
            lo, hi = (self._policy.between_cards_min,
                      self._policy.between_cards_max)
        delay = random.uniform(lo, hi)
        self._interruptible_sleep(delay)

    def _interruptible_sleep(self, seconds: float) -> None:
        """Спит, но просыпается на остановку воркера (не блокирует SIGTERM надолго)."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(2.0, end - time.time()))

    def _commit(self, message) -> None:
        tp = TopicPartition(message.topic, message.partition)
        self.consumer.commit({tp: OffsetAndMetadata(message.offset + 1, None, -1)})

    def _close_consumer(self):
        if self.consumer is not None:
            try:
                self.consumer.close()
            except Exception as e:
                logger.warning("Ошибка при закрытии консьюмера: %s", e)

    def _shutdown(self):
        logger.info("Останавливаю воркер...")
        self._close_consumer()
        try:
            self.service.close_crawl()   # закрыть живой браузер сессии обхода
        except Exception as e:
            logger.debug("Ошибка закрытия сессии обхода: %s", e)
        self.producer.close()
        logger.info("Воркер остановлен")