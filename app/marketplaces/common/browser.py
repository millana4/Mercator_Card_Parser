# -*- coding: utf-8 -*-
"""
browser.py — общий запуск stealth-Chrome для площадок.

Конфигурация та же, что проходит антибот Ozon (ozon/parser.py пока работает
со своей копией; перевод Ozon на этот модуль — этап W1). Профиль Chrome у каждой
площадки свой: два браузера не могут одновременно открыть один профиль.
"""
import os
import tempfile

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium_stealth import stealth

from app.core.logging import get_logger

logger = get_logger(__name__)

CHROMEDRIVER_PATH = "/usr/local/bin/chromedriver"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def clear_stale_locks(profile_dir: str) -> None:
    """Убрать залипшие Chrome-локи от некорректно закрытого прошлого запуска,
    иначе новый Chrome не стартует на том же профиле."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(profile_dir, name)
        try:
            if os.path.islink(path) or os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.debug("Не удалось убрать лок %s: %s", path, e)


def prepare_profile_dir(env_var: str) -> tuple[str, bool]:
    """Каталог профиля Chrome: постоянный из env_var (кука антибота переживает
    перезапуски) или временный. Возвращает (путь, временный_ли)."""
    persistent = os.environ.get(env_var)
    if persistent:
        os.makedirs(persistent, exist_ok=True)
        clear_stale_locks(persistent)
        return persistent, False
    return tempfile.mkdtemp(), True


def build_stealth_driver(headless: bool, profile_dir: str) -> webdriver.Chrome:
    """Chrome со stealth-настройками и указанным профилем."""
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-features=UserAgentClientHint")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-agent={USER_AGENT}")
    opts.add_argument(f"--user-data-dir={profile_dir}")

    driver = webdriver.Chrome(service=Service(CHROMEDRIVER_PATH), options=opts)
    stealth(
        driver,
        languages=["ru-RU", "ru", "en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
        run_on_insecure_origins=False,
    )
    logger.info("Chrome запущен (headless=%s, профиль=%s)", headless, profile_dir)
    return driver