# -*- coding: utf-8 -*-
"""
redis_config.py — настройки Redis-транспорта воркера.

Ключи списков: <поток>:<tasks|results>:<marketplace>:<geo>, например
parse:tasks:ozon:spb. Маркетплейс разводит площадки, гео — регионы.

Воркер читает только те площадки, что перечислены в PARSER_MARKETPLACES:
так один парсер может обслуживать и Ozon, и Детский мир, а другой — только
Детский мир. Список задаётся через запятую, порядок = приоритет.

parser_id — идентификатор воркера для наблюдаемости (логи ядра + счётчики).
"""
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RedisWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Подключение к Redis ---
    host: str = Field(default="localhost", alias="REDIS_HOST")
    port: int = Field(default=6379, alias="REDIS_PORT")
    db: int = Field(default=0, alias="REDIS_DB")
    password: str | None = Field(default=None, alias="REDIS_PASSWORD")

    # --- Регион ---
    geo_code: str = Field(default="spb", alias="OZON_GEO_CODE")
    geo_name: str = Field(default="Санкт-Петербург", alias="OZON_GEO_NAME")

    # --- Маркетплейсы, которые обслуживает этот воркер ---
    # "ozon" | "ozon,detmir" — порядок задаёт приоритет при выборе задачи.
    marketplaces_raw: str = Field(default="ozon", alias="PARSER_MARKETPLACES")

    # Роль парсера: primary — быстрый темп; reserve — щадящий (1 обход/сут).
    role: str = Field(default="primary", alias="PARSER_ROLE")

    # Идентификатор воркера для наблюдаемости (в результатах и счётчиках).
    parser_id: str = Field(default="0", alias="PARSER_ID")

    # --- Тайминги ---
    # BRPOP блокируется на этот таймаут, потом воркер проверяет флаг остановки.
    brpop_timeout_s: int = Field(default=2, alias="REDIS_BRPOP_TIMEOUT_S")
    reconnect_delay_s: int = Field(default=5, alias="REDIS_RECONNECT_DELAY_S")

    # --- Префиксы ключей (маркетплейс и гео добавляются в свойствах ниже) ---
    parse_tasks_prefix: str = Field(default="parse:tasks", alias="REDIS_PARSE_TASKS_PREFIX")
    parse_results_prefix: str = Field(default="parse:results", alias="REDIS_PARSE_RESULTS_PREFIX")
    select_tasks_prefix: str = Field(default="select:tasks", alias="REDIS_SELECT_TASKS_PREFIX")
    select_results_prefix: str = Field(default="select:results", alias="REDIS_SELECT_RESULTS_PREFIX")
    search_tasks_prefix: str = Field(default="search:tasks", alias="REDIS_SEARCH_TASKS_PREFIX")
    search_results_prefix: str = Field(default="search:results", alias="REDIS_SEARCH_RESULTS_PREFIX")

    @field_validator("marketplaces_raw")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("PARSER_MARKETPLACES не должен быть пустым "
                             "(например: PARSER_MARKETPLACES=ozon,detmir)")
        return v

    # ------------------------------------------------------------------ #
    @property
    def marketplaces(self) -> list[str]:
        """Список кодов площадок этого воркера, в порядке приоритета."""
        return [m.strip() for m in self.marketplaces_raw.split(",") if m.strip()]

    # --- Построение ключей ---
    def select_tasks_key(self, marketplace: str) -> str:
        return f"{self.select_tasks_prefix}:{marketplace}:{self.geo_code}"

    def select_results_key(self, marketplace: str) -> str:
        return f"{self.select_results_prefix}:{marketplace}:{self.geo_code}"

    def parse_tasks_key(self, marketplace: str) -> str:
        return f"{self.parse_tasks_prefix}:{marketplace}:{self.geo_code}"

    def parse_results_key(self, marketplace: str) -> str:
        return f"{self.parse_results_prefix}:{marketplace}:{self.geo_code}"

    def search_tasks_key(self, marketplace: str) -> str:
        return f"{self.search_tasks_prefix}:{marketplace}:{self.geo_code}"

    def search_results_key(self, marketplace: str) -> str:
        return f"{self.search_results_prefix}:{marketplace}:{self.geo_code}"

    @property
    def task_keys(self) -> list[str]:
        """Ключи задач для BRPOP. Порядок = приоритет: подбор, снимок выдачи, обход.
        Redis отдаёт из первого непустого ключа. Снимок выдачи стоит перед обходом:
        в очереди обхода всегда есть буфер, иначе выдача не выполнилась бы никогда."""
        select_keys = [self.select_tasks_key(m) for m in self.marketplaces]
        search_keys = [self.search_tasks_key(m) for m in self.marketplaces]
        parse_keys = [self.parse_tasks_key(m) for m in self.marketplaces]
        return select_keys + search_keys + parse_keys

    @property
    def parse_task_keys(self) -> list[str]:
        """Ключи только очередей обхода: пробная задача после паузы берётся отсюда."""
        return [self.parse_tasks_key(m) for m in self.marketplaces]

    @property
    def key_routes(self) -> dict[str, tuple[str, str]]:
        """Разбор ключа задачи: ключ -> (поток, маркетплейс).
        Нужен, чтобы понять, чем обрабатывать задачу и куда класть результат."""
        routes: dict[str, tuple[str, str]] = {}
        for m in self.marketplaces:
            routes[self.select_tasks_key(m)] = ("select", m)
            routes[self.search_tasks_key(m)] = ("search", m)
            routes[self.parse_tasks_key(m)] = ("parse", m)
        return routes

    # --- Счётчики наблюдаемости (свои на каждую площадку) ---
    def counter_done(self, marketplace: str) -> str:
        return f"parser:{self.parser_id}:{marketplace}:done"

    def counter_antibot(self, marketplace: str) -> str:
        return f"parser:{self.parser_id}:{marketplace}:antibot"

    def counter_search_done(self, marketplace: str) -> str:
        return f"parser:{self.parser_id}:{marketplace}:search_done"

    def counter_search_antibot(self, marketplace: str) -> str:
        return f"parser:{self.parser_id}:{marketplace}:search_antibot"

    def counter_load_failed(self, marketplace: str) -> str:
        return f"parser:{self.parser_id}:{marketplace}:load_failed"


redis_worker_settings = RedisWorkerSettings()