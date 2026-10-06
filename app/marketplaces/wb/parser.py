# -*- coding: utf-8 -*-
"""
parser.py — низкоуровневый движок Wildberries: stealth-Chrome, открыть страницу
как живой пользователь, дождаться прогрузки, вернуть HTML.

Сейчас (W0) — только получение страницы для разведки. Разбор данных,
распознавание блокировки и долгоживущая сессия — следующие этапы.
"""
import random
import shutil
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

from app.core.config import MarketplacePolicy
from app.core.logging import get_logger
from app.marketplaces.common.browser import build_stealth_driver, prepare_profile_dir

logger = get_logger(__name__)


class WbParser:
    """Драйвер Chrome для страниц Wildberries."""

    HOME_URL = "https://www.wildberries.ru/"

    def __init__(self, policy: MarketplacePolicy):
        self.policy = policy
        self.driver = None
        self.profile_dir, self._profile_is_temp = prepare_profile_dir("WB_CHROME_PROFILE_DIR")
        try:
            self.driver = build_stealth_driver(policy.headless, self.profile_dir)
            self.driver.set_page_load_timeout(policy.page_load_timeout)
        except Exception:
            logger.error("Ошибка инициализации драйвера WB", exc_info=True)
            self.close()
            raise

    # ------------------------------------------------------------------ #
    def _pause(self) -> None:
        """Случайная пауза между действиями (антибот)."""
        time.sleep(random.uniform(self.policy.pause_min, self.policy.pause_max))

    def _on_wb(self) -> bool:
        try:
            return "wildberries.ru" in (self.driver.current_url or "")
        except Exception:
            return False

    def _wait_ready(self) -> None:
        """Дождаться document.readyState == 'complete' (не дольше wait_timeout)."""
        try:
            WebDriverWait(self.driver, self.policy.wait_timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete")
        except Exception:
            logger.warning("Страница не дошла до readyState=complete за %s сек",
                           self.policy.wait_timeout)

    def _scroll_through(self, steps: int = 4) -> None:
        """Прокрутить страницу порциями вниз и вернуться наверх:
        WB подгружает часть блоков только при прокрутке."""
        try:
            for _ in range(steps):
                self.driver.execute_script(f"window.scrollBy(0, {random.randint(600, 1000)});")
                time.sleep(random.uniform(0.8, 1.6))
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(random.uniform(0.5, 1.0))
        except Exception as e:
            logger.debug("Прокрутка не удалась: %s", e)

    def visit_home(self) -> None:
        """Зайти на главную WB и осмотреться, как человек."""
        logger.info("Захожу на главную Wildberries")
        try:
            self.driver.get(self.HOME_URL)
        except Exception as e:
            logger.warning("Ошибка загрузки главной WB: %s", e)
        self._wait_ready()
        self._pause()
        self._scroll_through(steps=2)

    def open_page(self, url: str, extra_wait_s: float) -> dict:
        """Открыть страницу изнутри WB (браузер сам ставит Referer) и вернуть
        {html, final_url, title, screenshot_png}."""
        if not self._on_wb():
            self.visit_home()
        self._pause()

        old_root = self.driver.find_element(By.TAG_NAME, "html")
        logger.info("Перехожу на страницу WB: %s", url)
        try:
            self.driver.execute_script("window.location.assign(arguments[0]);", url)
            # старая страница выгрузилась — дальше ждём готовности новой
            WebDriverWait(self.driver, self.policy.page_load_timeout).until(ec.staleness_of(old_root))
        except Exception as e:
            logger.warning("Переход на страницу не подтвердился: %s", e)
        self._wait_ready()

        time.sleep(extra_wait_s)  # WB дорисовывает содержимое скриптами
        self._scroll_through()

        return {
            "html": self.driver.page_source,
            "final_url": self.driver.current_url,
            "title": self.driver.title,
            "screenshot_png": self.screenshot_png(),
        }

    def screenshot_png(self) -> bytes | None:
        try:
            return self.driver.get_screenshot_as_png()
        except Exception as e:
            logger.debug("Скриншот не удался: %s", e)
            return None

    def close(self) -> None:
        """Закрыть драйвер. Постоянный профиль сохраняем, временный удаляем."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.debug("Ошибка quit(): %s", e)
            self.driver = None
        if self._profile_is_temp and self.profile_dir:
            shutil.rmtree(self.profile_dir, ignore_errors=True)