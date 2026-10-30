# -*- coding: utf-8 -*-
"""
config.py — единый источник настроек сервиса.

Все значения берутся из переменных окружения (.env) через os.getenv.
В коде — только дефолты-страховки. Чтобы поменять настройку (например, паузы),
правь ТОЛЬКО .env — код ничего дублировать не заставляет.
"""

import os
from dataclasses import dataclass


def _get_float(key: str, default: float) -> float:
    """Прочитать float из env; если нет или битое — вернуть дефолт."""
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_str(key: str, default: str) -> str:
    val = os.getenv(key)
    return val if val is not None and val.strip() != "" else default


def _get_choice(key: str, default: str, choices: tuple[str, ...]) -> str:
    """Значение из списка допустимых (без учёта регистра); иначе — дефолт."""
    val = _get_str(key, default).strip().lower()
    return val if val in choices else default


@dataclass
class MarketplacePolicy:
    """Политика парсинга одного маркетплейса: таймауты, паузы, ретраи."""
    headless: bool                      # запускать браузер без окна (True) или с окном (False)
    page_load_timeout: int              # сек: сколько ждать загрузки страницы, потом ошибка
    wait_timeout: int                   # сек: сколько ждать появления данных (__NUXT__) на странице
    pause_min: float                    # сек: мин. пауза между действиями ВНУТРИ карточки (антибот)
    pause_max: float                    # сек: макс. пауза между действиями внутри карточки
    retries: int                        # сколько раз повторить попытку при сбое/блокировке
    retry_pause_min: float              # сек: мин. пауза перед повторной попыткой (только при сбое)
    retry_pause_max: float              # сек: макс. пауза перед повторной попыткой
    between_cards_min: float            # сек: мин. пауза МЕЖДУ карточками (обычный парсер, primary)
    between_cards_max: float            # сек: макс. пауза между карточками (primary)
    reserve_between_cards_min: float    # сек: мин. пауза между карточками для запасного парсера (reserve, щадящий темп)
    reserve_between_cards_max: float    # сек: макс. пауза между карточками (reserve)
    load_failure_threshold: int         # сколько сбоев загрузки подряд считать баном
    ban_pause_start_s: float            # сек: первая пауза после бана; каждая следующая ×2
    ban_pause_max_s: float              # сек: потолок паузы после бана
    ban_level_reset_s: float            # сек: столько без бана после конца паузы — уровень обнуляется
    block_artifacts_keep: int           # сколько последних блокировок хранить (json + png) в debug_dir
    session_min: int                    # мин. число карточек в одной сессии браузера до пересоздания
    session_max: int                    # макс. число карточек в сессии браузера
    min_rating: float                   # порог отбора: мин. рейтинг карточки-кандидата при подборе
    min_reviews: int                    # порог отбора: мин. число отзывов (строго больше) при подборе
    pool_multiplier: int                # подбор: собирать пул кандидатов = нужно × этот множитель
    max_pages: int                      # подбор: потолок прокруток страницы выдачи
    search_tiles_target: int = 32       # снимок выдачи: сколько плиток набирать прокруткой
    search_max_scrolls: int = 10        # снимок выдачи: потолок прокруток
    full_content_timeout: float = 15.0  # сек: сколько ждать полный блок характеристик при полном разборе
    search_task_max_age_s: float = 21600.0  # сек: задача выдачи старше (от run_at) не выполняется
    search_idle_close_s: float = 900.0  # сек: браузер выдачи без задач выдачи и подбора столько — закрывается (профиль остаётся)
    # Переключатели подмен отпечатка (опыт по одной; по умолчанию — как сейчас)
    fp_webgl: str = "spoof"             # spoof — WebGL + строка видеокарты профиля; native — WebGL без подмены; off — WebGL выключен
    fp_tostring_proxy: bool = True      # Proxy на Function.prototype.toString (подмены выглядят встроенными)
    fp_client_hints: bool = True        # Client Hints Windows через CDP (userAgentMetadata)


class Settings:
    """Глобальные настройки сервиса. Всё — из env через os.getenv."""

    def __init__(self):
        # Общие
        self.debug = _get_bool("DEBUG", True)
        self.log_level = _get_str("LOG_LEVEL", "DEBUG")
        self.debug_dir = _get_str("DEBUG_DIR", "app/debug")
        self.api_prefix = _get_str("API_PREFIX", "/api/v1")
        self.service_api_key = _get_str("SERVICE_API_KEY", "")

    def ozon_policy(self) -> MarketplacePolicy:
        """Политика Ozon — все значения из env (OZON_*)."""
        return MarketplacePolicy(
            headless=_get_bool("OZON_HEADLESS", False),
            page_load_timeout=_get_int("OZON_PAGE_LOAD_TIMEOUT", 30),
            wait_timeout=_get_int("OZON_WAIT_TIMEOUT", 15),
            pause_min=_get_float("OZON_PAUSE_MIN", 15.0),
            pause_max=_get_float("OZON_PAUSE_MAX", 30.0),
            retries=_get_int("OZON_RETRIES", 2),
            retry_pause_min=_get_float("OZON_RETRY_PAUSE_MIN", 60.0),
            retry_pause_max=_get_float("OZON_RETRY_PAUSE_MAX", 80.0),
            between_cards_min=_get_float("OZON_BETWEEN_CARDS_MIN", 50.0),
            between_cards_max=_get_float("OZON_BETWEEN_CARDS_MAX", 90.0),
            reserve_between_cards_min=_get_float("OZON_RESERVE_BETWEEN_CARDS_MIN", 50.0),
            reserve_between_cards_max=_get_float("OZON_RESERVE_BETWEEN_CARDS_MAX", 80.0),
            load_failure_threshold=_get_int("OZON_LOAD_FAILURE_THRESHOLD", 3),
            ban_pause_start_s=_get_float("OZON_BAN_PAUSE_START_S", 600.0),
            ban_pause_max_s=_get_float("OZON_BAN_PAUSE_MAX_S", 7200.0),
            ban_level_reset_s=_get_float("OZON_BAN_LEVEL_RESET_S", 21600.0),
            search_idle_close_s=_get_float("OZON_SEARCH_IDLE_CLOSE_S", 900.0),
            block_artifacts_keep=_get_int("OZON_BLOCK_ARTIFACTS_KEEP", 50),
            session_min=_get_int("OZON_SESSION_MIN", 20),
            session_max=_get_int("OZON_SESSION_MAX", 50),
            min_rating=_get_float("OZON_MIN_RATING", 4.8),
            min_reviews=_get_int("OZON_MIN_REVIEWS", 30),
            pool_multiplier=_get_int("OZON_POOL_MULTIPLIER", 3),
            max_pages=_get_int("OZON_MAX_PAGES", 10),
            search_tiles_target=_get_int("OZON_SEARCH_TILES_TARGET", 32),
            search_max_scrolls=_get_int("OZON_SEARCH_MAX_SCROLLS", 10),
            full_content_timeout=_get_float("OZON_FULL_CONTENT_TIMEOUT", 15.0),
            search_task_max_age_s=_get_float("OZON_SEARCH_TASK_MAX_AGE_S", 21600.0),
            fp_webgl=_get_choice("OZON_FP_WEBGL", "spoof", ("spoof", "native", "off")),
            fp_tostring_proxy=_get_bool("OZON_FP_TOSTRING_PROXY", True),
            fp_client_hints=_get_bool("OZON_FP_CLIENT_HINTS", True),
        )


# Единственный экземпляр настроек на весь сервис.
settings = Settings()