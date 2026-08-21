# -*- coding: utf-8 -*-
"""
redis_config.py — настройки Redis-транспорта воркера.
Ключи списков: <поток>:<tasks|results>:<geo_code>, например parse:tasks:spb.
Гео разводит регионы (парсер региона читает только свой ключ). parser_id —
идентификатор воркера для наблюдаемости (логи ядра + счётчики в Redis).
При добавлении региона поднимается воркер с другим OZON_GEO_CODE.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class RedisWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Подключение к Redis ---
    host: str = Field(default="localhost", alias="REDIS_HOST")
    port: int = Field(default=6379, alias="REDIS_PORT")
    db: int = Field(default=0, alias="REDIS_DB")

    # --- Регион ---
    geo_code: str = Field(default="spb", alias="OZON_GEO_CODE")
    geo_name: str = Field(default="Санкт-Петербург", alias="OZON_GEO_NAME")

    # Роль парсера: primary — быстрый темп; reserve — щадящий (1 обход/сут).
    role: str = Field(default="primary", alias="PARSER_ROLE")
    # Идентификатор воркера для наблюдаемости (в результатах и счётчиках).
    parser_id: str = Field(default="0", alias="PARSER_ID")

    # --- Тайминги ---
    # BRPOP блокируется на этот таймаут, потом воркер проверяет флаг остановки.
    brpop_timeout_s: int = Field(default=2, alias="REDIS_BRPOP_TIMEOUT_S")
    reconnect_delay_s: int = Field(default=5, alias="REDIS_RECONNECT_DELAY_S")

    # --- Префиксы ключей (гео добавляется в свойствах ниже) ---
    parse_tasks_prefix: str = Field(default="parse:tasks", alias="REDIS_PARSE_TASKS_PREFIX")
    parse_results_prefix: str = Field(default="parse:results", alias="REDIS_PARSE_RESULTS_PREFIX")
    select_tasks_prefix: str = Field(default="select:tasks", alias="REDIS_SELECT_TASKS_PREFIX")
    select_results_prefix: str = Field(default="select:results", alias="REDIS_SELECT_RESULTS_PREFIX")

    @property
    def select_tasks_key(self) -> str:
        return f"{self.select_tasks_prefix}:{self.geo_code}"

    @property
    def select_results_key(self) -> str:
        return f"{self.select_results_prefix}:{self.geo_code}"

    @property
    def parse_tasks_key(self) -> str:
        return f"{self.parse_tasks_prefix}:{self.geo_code}"

    @property
    def parse_results_key(self) -> str:
        return f"{self.parse_results_prefix}:{self.geo_code}"

    @property
    def task_keys(self) -> list[str]:
        # Порядок = приоритет BRPOP: подбор важнее обхода.
        return [self.select_tasks_key, self.parse_tasks_key]

    @property
    def counter_done(self) -> str:
        return f"parser:{self.parser_id}:done"

    @property
    def counter_antibot(self) -> str:
        return f"parser:{self.parser_id}:antibot"


redis_worker_settings = RedisWorkerSettings()