# -*- coding: utf-8 -*-
"""
result_producer.py — отправка результата задачи в Redis-список результатов.
Значение — строка UTF-8 с JSON в snake_case (LPUSH). Ядро читает через BRPOP.
Подключение отказоустойчивое: ждём Redis, а не падаем.
"""
import json
import time
import redis
from app.core.logging import get_logger
from app.worker.redis_config import redis_worker_settings as rws

logger = get_logger(__name__)


class ResultProducer:
    def __init__(self):
        self._redis = self._connect_with_retry()

    def _connect_with_retry(self) -> redis.Redis:
        """Подключиться к Redis, повторяя попытки, пока он не станет доступен."""
        attempt = 0
        while True:
            attempt += 1
            try:
                client = redis.Redis(
                    host=rws.host, port=rws.port, db=rws.db,
                    password=rws.password,
                    decode_responses=True,
                    socket_connect_timeout=10,
                )
                client.ping()
                logger.info("ResultProducer подключён к Redis %s:%s (попытка %s)",
                            rws.host, rws.port, attempt)
                return client
            except Exception as e:
                logger.warning("Redis недоступен (%s:%s): %s — жду, попытка %s",
                               rws.host, rws.port, e, attempt)
                time.sleep(rws.reconnect_delay_s)

    def send(self, key: str, result: dict) -> None:
        """Положить результат в список результатов (LPUSH). Бросает при ошибке —
        consumer это ловит и логирует (задачу ядро переотправит по таймауту)."""
        try:
            payload = json.dumps(result, ensure_ascii=False)
            self._redis.lpush(key, payload)
            logger.info("Результат отправлен в %s | task_id=%s ok=%s",
                        key, result.get("task_id"), result.get("ok"))
        except Exception as e:
            logger.error("Не удалось отправить результат в %s | task_id=%s: %s",
                         key, result.get("task_id"), e)
            raise

    def incr_counter(self, key: str) -> None:
        """Инкремент счётчика наблюдаемости (best-effort, ошибку глотаем)."""
        try:
            self._redis.incr(key)
        except Exception as e:
            logger.debug("Не удалось инкрементить счётчик %s: %s", key, e)

    def close(self) -> None:
        try:
            self._redis.close()
            logger.info("ResultProducer закрыт")
        except Exception as e:
            logger.warning("Ошибка при закрытии ResultProducer: %s", e)