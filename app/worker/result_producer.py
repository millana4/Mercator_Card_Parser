# -*- coding: utf-8 -*-
"""
result_producer.py — отправка результата задачи в топик результатов.
Значение — строка UTF-8 с JSON в snake_case. Ключ не используется.
Подключение к брокеру отказоустойчивое: ждём брокер, а не падаем.
"""

import json
import time
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable, KafkaError

from app.core.logging import get_logger
from app.worker.kafka_config import kafka_worker_settings as kws

logger = get_logger(__name__)


class ResultProducer:
    def __init__(self):
        self._producer = self._connect_with_retry()

    def _connect_with_retry(self) -> KafkaProducer:
        """Подключиться к брокеру, повторяя попытки, пока он не станет доступен."""
        attempt = 0
        while True:
            attempt += 1
            try:
                producer = KafkaProducer(
                    bootstrap_servers=kws.bootstrap_servers,
                    value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
                    acks="all",
                    retries=3,
                    **kws.kafka_auth_kwargs,
                )
                logger.info("ResultProducer подключён к %s (попытка %s)",
                            kws.bootstrap_servers, attempt)
                return producer
            except NoBrokersAvailable:
                logger.warning("Брокер недоступен (%s), жду... попытка %s",
                               kws.bootstrap_servers, attempt)
                time.sleep(kws.reconnect_delay_s)
            except Exception as e:
                logger.warning("Не удалось подключить продюсер (%s): %s — жду, попытка %s",
                               kws.bootstrap_servers, e, attempt)
                time.sleep(kws.reconnect_delay_s)

    def send(self, topic: str, result: dict) -> None:
        try:
            future = self._producer.send(topic, value=result)
            future.get(timeout=30)
            logger.info("Результат отправлен в %s | task_id=%s ok=%s",
                        topic, result.get("task_id"), result.get("ok"))
        except Exception as e:
            logger.error("Не удалось отправить результат в %s | task_id=%s: %s",
                         topic, result.get("task_id"), e)
            raise

    def close(self) -> None:
        try:
            self._producer.flush(timeout=10)
            self._producer.close(timeout=10)
            logger.info("ResultProducer закрыт")
        except Exception as e:
            logger.warning("Ошибка при закрытии ResultProducer: %s", e)