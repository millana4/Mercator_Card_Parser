# -*- coding: utf-8 -*-
"""
config.py — единый источник настроек сервиса (из переменных окружения).

Здесь же — политики таймаутов и ретраев. Сделано двухуровнево:
  - общие значения по умолчанию (DEFAULT_*),
  - переопределение для каждого маркетплейса (OZON_*, в будущем WB_*, YM_*).
Так у каждого маркетплейса своя политика, как ты и просила.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MarketplacePolicy(BaseSettings):
    """
    Политика парсинга одного маркетплейса: таймауты, паузы, ретраи.
    Заполняется из общих дефолтов и переопределяется env-переменными.
    """
    headless: bool = False
    page_load_timeout: int = 30          # сек, set_page_load_timeout
    wait_timeout: int = 15               # сек, ожидание появления данных на странице
    pause_min: float = 40.0               # мин. пауза между действиями (антибот)
    pause_max: float = 60.0               # макс. пауза между действиями
    retries: int = 3                     # число попыток при блокировке/ошибке
    retry_pause_min: float = 60.0        # пауза между попытками, мин
    retry_pause_max: float = 80.0        # пауза между попытками, макс
    # Темп воркера и реакция на серию блокировок
    between_cards_min: float = 120.0  # пауза между карточками (primary), мин
    between_cards_max: float = 180.0  # пауза между карточками (primary), макс
    # Щадящий темп запасного парсера (reserve): ~1 полный обход 323 карточек/сутки.
    # 24ч / 323 ≈ 267с на карточку; диапазон вокруг для случайности.
    reserve_between_cards_min: float = 240.0
    reserve_between_cards_max: float = 300.0
    antibot_streak_threshold: int = 3    # сколько блокировок подряд = «полоса»
    antibot_cooldown_s: int = 600        # сон воркера при полосе, сек (10 мин)
    # Количество карточек в сессии браузера
    session_min: int = 10
    session_max: int = 50
    # Пороги отбора карточек при подборе
    min_rating: float = 4.8  # минимальный рейтинг кандидата
    min_reviews: int = 100  # минимальное число отзывов (строго больше)
    # Подбор: сбор пула кандидатов из выдачи
    pool_multiplier: int = 3  # целевой пул плиток = count * этот множитель
    max_pages: int = 10  # потолок прокруток выдачи


class Settings(BaseSettings):
    """Глобальные настройки сервиса."""
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Режим отладки. В DEBUG сохраняются html/json/png и доступны файловые эндпоинты.
    debug: bool = Field(default=True, alias="DEBUG")

    # Уровень логирования: DEBUG / INFO / WARNING / ERROR.
    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL")

    # Куда складывать отладочные артефакты (только при debug=true).
    debug_dir: str = Field(default="app/debug", alias="DEBUG_DIR")

    # Версия API в префиксе путей.
    api_prefix: str = Field(default="/api/v1", alias="API_PREFIX")

    # Ключ для межсервисной авторизации. Сейчас не проверяется (заглушка).
    service_api_key: str = Field(default="", alias="SERVICE_API_KEY")

    # --- Kafka (журнал событий). Если брокер недоступен — сервис работает без журнала. ---
    kafka_enabled: bool = Field(default=False, alias="KAFKA_ENABLED")
    kafka_bootstrap_servers: str = Field(default="localhost:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    kafka_topic: str = Field(default="parsing-events", alias="KAFKA_TOPIC")

    # --- Политики маркетплейсов ---
    # Ozon: можно переопределить через env OZON_HEADLESS, OZON_RETRIES и т.д.
    ozon_headless: bool = Field(default=False, alias="OZON_HEADLESS")
    ozon_page_load_timeout: int = Field(default=30, alias="OZON_PAGE_LOAD_TIMEOUT")
    ozon_wait_timeout: int = Field(default=15, alias="OZON_WAIT_TIMEOUT")
    ozon_pause_min: float = Field(default=40.0, alias="OZON_PAUSE_MIN")
    ozon_pause_max: float = Field(default=60.0, alias="OZON_PAUSE_MAX")
    ozon_retries: int = Field(default=3, alias="OZON_RETRIES")
    ozon_retry_pause_min: float = Field(default=60.0, alias="OZON_RETRY_PAUSE_MIN")
    ozon_retry_pause_max: float = Field(default=80.0, alias="OZON_RETRY_PAUSE_MAX")
    ozon_between_cards_min: float = Field(default=120.0, alias="OZON_BETWEEN_CARDS_MIN")
    ozon_between_cards_max: float = Field(default=180.0, alias="OZON_BETWEEN_CARDS_MAX")
    ozon_reserve_between_cards_min: float = Field(default=240.0, alias="OZON_RESERVE_BETWEEN_CARDS_MIN")
    ozon_reserve_between_cards_max: float = Field(default=300.0, alias="OZON_RESERVE_BETWEEN_CARDS_MAX")
    ozon_antibot_streak_threshold: int = Field(default=3, alias="OZON_ANTIBOT_STREAK_THRESHOLD")
    ozon_antibot_cooldown_s: int = Field(default=600, alias="OZON_ANTIBOT_COOLDOWN_S")
    ozon_session_min: int = Field(default=10, alias="OZON_SESSION_MIN")
    ozon_session_max: int = Field(default=50, alias="OZON_SESSION_MAX")
    ozon_min_rating: float = Field(default=4.8, alias="OZON_MIN_RATING")
    ozon_min_reviews: int = Field(default=100, alias="OZON_MIN_REVIEWS")
    ozon_pool_multiplier: int = Field(default=3, alias="OZON_POOL_MULTIPLIER")
    ozon_max_pages: int = Field(default=10, alias="OZON_MAX_PAGES")
    service_api_key: str = Field(default="", alias="SERVICE_API_KEY")

    def ozon_policy(self) -> MarketplacePolicy:
        """Собрать политику Ozon из настроек."""
        return MarketplacePolicy(
            headless=self.ozon_headless,
            page_load_timeout=self.ozon_page_load_timeout,
            wait_timeout=self.ozon_wait_timeout,
            pause_min=self.ozon_pause_min,
            pause_max=self.ozon_pause_max,
            retries=self.ozon_retries,
            retry_pause_min=self.ozon_retry_pause_min,
            retry_pause_max=self.ozon_retry_pause_max,
            between_cards_min=self.ozon_between_cards_min,
            between_cards_max=self.ozon_between_cards_max,
            reserve_between_cards_min=self.ozon_reserve_between_cards_min,
            reserve_between_cards_max=self.ozon_reserve_between_cards_max,
            antibot_streak_threshold=self.ozon_antibot_streak_threshold,
            antibot_cooldown_s=self.ozon_antibot_cooldown_s,
            session_min=self.ozon_session_min,
            session_max=self.ozon_session_max,
            min_rating=self.ozon_min_rating,
            min_reviews=self.ozon_min_reviews,
            pool_multiplier=self.ozon_pool_multiplier,
            max_pages=self.ozon_max_pages,
        )


# Единственный экземпляр настроек на весь сервис.
settings = Settings()
