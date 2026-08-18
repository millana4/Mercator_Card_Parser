# -*- coding: utf-8 -*-
"""
parser.py — низкоуровневый движок парсинга Ozon.

Ответственность: открыть страницу в stealth-Chrome, дождаться данных, вернуть
HTML и извлечённый объект __NUXT__.state. Ничего не сохраняет на диск и не
собирает доменную карточку — это делают слои выше (repository/service/mapper).

Таймауты, паузы и ретраи берутся из MarketplacePolicy (см. core/config.py),
поэтому у Ozon своя политика, а у будущих WB/Я.Маркет — своя.
"""

import re
import json
import time
import random
import shutil
import tempfile
import os

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium_stealth import stealth

from app.core.config import MarketplacePolicy
from app.core.logging import get_logger
from app.core.exceptions import AntibotBlockedError
from app.marketplaces.ozon.nuxt import extract_nuxt_state

logger = get_logger(__name__)


class OzonParser:
    """Драйвер + получение HTML + извлечение __NUXT__ для одной карточки Ozon."""

    def __init__(self, policy: MarketplacePolicy):
        self.policy = policy
        self.driver = None
        self.wait = None
        # Постоянный профиль (том): кука-пропуск антибота переживает карточки и
        # рестарты. Если путь не задан — временный профиль.
        persistent = os.environ.get("OZON_CHROME_PROFILE_DIR")
        if persistent:
            os.makedirs(persistent, exist_ok=True)
            self.chrome_temp_dir = persistent
            self._profile_is_temp = False
            self._clear_stale_locks(persistent)
        else:
            self.chrome_temp_dir = tempfile.mkdtemp()
            self._profile_is_temp = True
        logger.debug("OzonParser.__init__: профиль Chrome=%s (temp=%s)",
                     self.chrome_temp_dir, self._profile_is_temp)
        try:
            self._setup_driver()
            self.wait = WebDriverWait(self.driver, policy.wait_timeout)
        except Exception:
            logger.error("Ошибка инициализации драйвера", exc_info=True)
            self.close()
            raise
        # Счётчик сессии: сколько карточек ещё можно открыть до пересоздания.
        self._session_left = 0
        self._session_started = False

    # ------------------------------------------------------------------ #
    @staticmethod
    def _clear_stale_locks(profile_dir: str) -> None:
        """Убрать залипшие Chrome-локи от некорректно закрытого прошлого запуска
        иначе новый Chrome не стартует на том же профиле."""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = os.path.join(profile_dir, name)
            try:
                if os.path.islink(path) or os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.debug("Не удалось убрать лок %s: %s", path, e)

    # ------------------------------------------------------------------ #
    def _setup_driver(self):
        """Stealth-конфигурация Chrome (та, что проходит антибот Ozon)."""
        logger.debug("_setup_driver: headless=%s", self.policy.headless)
        opts = Options()
        if self.policy.headless:
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
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        )
        opts.add_argument(f"--user-data-dir={self.chrome_temp_dir}")

        service = Service("/usr/local/bin/chromedriver")
        self.driver = webdriver.Chrome(service=service, options=opts)
        stealth(
            self.driver,
            languages=["ru-RU", "ru", "en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
            run_on_insecure_origins=False,
        )
        logger.info("Драйвер Chrome инициализирован (headless=%s)", self.policy.headless)

    # ------------------------------------------------------------------ #
    def _pause(self):
        """Случайная пауза между действиями (антибот)."""
        time.sleep(random.uniform(self.policy.pause_min, self.policy.pause_max))

    def _random_mouse_movements(self):
        """Имитация движений мыши и скролла."""
        try:
            size = self.driver.get_window_size()
            w, h = size["width"], size["height"]
            x1, y1 = random.randint(100, w - 100), random.randint(100, h - 100)
            x2, y2 = random.randint(100, w - 100), random.randint(100, h - 100)
            actions = webdriver.ActionChains(self.driver)
            actions.move_by_offset(x1, y1).pause(random.uniform(0.1, 0.3))
            actions.move_by_offset(x2 - x1, y2 - y1).pause(random.uniform(0.1, 0.3))
            actions.perform()
            self.driver.execute_script(f"window.scrollBy(0, {random.randint(200, 800)});")
            time.sleep(random.uniform(0.5, 1.5))
        except Exception:
            logger.debug("Движение мыши не удалось (вне окна) — пропускаю")

    def visit_home(self) -> None:
        """Начать сессию как человек: зайти на главную Ozon, осмотреться.
        Кука с главной ложится в постоянный профиль, референс перехода на
        карточку становится «человеческим» (не заход по прямому URL из ниоткуда)."""
        try:
            self.driver.set_page_load_timeout(self.policy.page_load_timeout)
            logger.info("Старт сессии: захожу на главную Ozon")
            self.driver.get("https://www.ozon.ru/")
            self._pause()
            self._random_mouse_movements()
            self._pause()
        except Exception as e:
            logger.warning("Не удалось открыть главную (сессия всё равно продолжится): %s", e)

    def _navigate_within_ozon(self, url: str) -> None:
        """Перейти на карточку КАК живой пользователь — через window.location
        со страницы Ozon, чтобы браузер сам проставил Referer=ozon.ru.
        Если браузер сейчас не на Ozon (первый заход/сбой) — обычный get,
        предварительно зайдя на главную."""
        try:
            cur = self.driver.current_url or ""
        except Exception:
            cur = ""

        on_ozon = "ozon.ru" in cur

        if not on_ozon:
            # Не на Ozon — заходим на главную, чтобы следующий переход имел Referer.
            logger.info("Браузер не на Ozon (url=%r) — открываю главную перед карточкой", cur)
            self.driver.get("https://www.ozon.ru/")
            self._pause()

        # Переход изнутри Ozon: браузer сам поставит Referer текущей страницы.
        # location.assign сохраняет реферер, в отличие от прямого driver.get.
        self.driver.execute_script("window.location.assign(arguments[0]);", url)

    def begin_session(self) -> None:
        """Начать новую сессию: случайный лимит переходов + заход на главную."""
        self._session_left = random.randint(self.policy.session_min,
                                            self.policy.session_max)
        self._session_started = True
        logger.info("Новая сессия браузера: лимит переходов=%s", self._session_left)
        self.visit_home()

    def session_exhausted(self) -> bool:
        """Сессия исчерпана — пора пересоздавать браузер."""
        return self._session_left <= 0

    def consume_session_step(self) -> None:
        """Отметить, что одна карточка в сессии израсходована."""
        self._session_left -= 1
        logger.debug("Сессия: осталось переходов=%s", self._session_left)

    def _is_blocked(self) -> bool:
        """Признаки капчи/блокировки: заголовок, URL, iframe капчи, поле ввода."""
        try:
            title = self.driver.title or ""
            cur_url = self.driver.current_url or ""
        except Exception:
            return False

        # 1. Быстрая проверка по заголовку и URL (ловит 99% капч)
        if "Antibot Captcha" in title or "__rr=1" in cur_url:
            logger.warning("Обнаружена капча/блокировка (title=%r, url=%r)", title, cur_url)
            return True

        # 2. iframe с капчей
        try:
            if self.driver.find_elements(By.XPATH, "//iframe[contains(@src, 'captcha')]"):
                logger.warning("Обнаружен iframe с капчей")
                return True
        except Exception:
            pass

        # 3. Поле ввода капчи (редко, но бывает)
        try:
            if self.driver.find_elements(By.XPATH, "//input[@name='captcha']"):
                logger.warning("Обнаружено поле ввода капчи")
                return True
        except Exception:
            pass

        return False

    def _is_not_found(self) -> bool:
        """Страница удалённого товара: Ozon отдаёт «Такой страницы не существует»
        (виджет data-widget="error"). Это НЕ блок и НЕ 'нет в наличии' —
        карточка удалена. Проверяем дёшево по page_source, без XPath-ретраев."""
        try:
            src = self.driver.page_source
        except Exception:
            return False
        return ('data-widget="error"' in src
                or "Такой страницы не существует" in src)

    def fetch_html(self, url: str) -> str:
        """
        Открыть карточку с ретраями и вернуть HTML.
        Бросает AntibotBlockedError, если все попытки упёрлись в блок.
        Регион определяется Ozon по IP окружения.
        """
        self.driver.set_page_load_timeout(self.policy.page_load_timeout)
        last_blocked = False

        for attempt in range(1, self.policy.retries + 1):
            logger.info("Открываю карточку (попытка %s/%s): %s",
                        attempt, self.policy.retries, url)
            self._pause()
            try:
                self.driver.get(url)
            except Exception as e:
                logger.warning("Ошибка загрузки страницы: %s", e)

            # --- Проверка на редирект на поиск (тихая блокировка) ---
            current_url = self.driver.current_url
            if "/search/" in current_url and "/product/" not in current_url:
                logger.warning("Редирект на поиск вместо карточки: %s", url)
                raise AntibotBlockedError("Редирект на поиск (антибот)", marketplace="ozon")

            if self._is_blocked():
                last_blocked = True
                logger.warning("Антибот заблокировал на попытке %s", attempt)
                if attempt < self.policy.retries:
                    time.sleep(random.uniform(self.policy.retry_pause_min,
                                              self.policy.retry_pause_max))
                    continue
                break

            self._pause()
            self._random_mouse_movements()
            self._random_mouse_movements()
            self._pause()

            try:
                self.wait.until(lambda d: "__NUXT__" in d.page_source)
                logger.info("Страница отдала __NUXT__ — данные получены")
                return self.driver.page_source
            except Exception:
                logger.warning("__NUXT__ не появился на попытке %s", attempt)
                last_blocked = True
                if attempt < self.policy.retries:
                    time.sleep(random.uniform(self.policy.retry_pause_min,
                                              self.policy.retry_pause_max))

        if last_blocked:
            raise AntibotBlockedError(
                "Не удалось получить страницу: антибот/блокировка.",
                marketplace="ozon",
            )
        return self.driver.page_source

    # ------------------------------------------------------------------ #
    # Извлечение __NUXT__ вынесено в nuxt.py (чистая функция, тестируемая
    # без браузера). Оставляем тонкую обёртку для обратной совместимости.
    extract_nuxt_state = staticmethod(extract_nuxt_state)

    # ------------------------------------------------------------------ #

    def fetch_html_in_session(self, url: str) -> str:
        """Открыть карточку в УЖЕ живом браузере (сессия переиспользуется)."""
        self.driver.set_page_load_timeout(self.policy.page_load_timeout)
        logger.info("Открываю карточку в сессии: %s", url)
        self._pause()
        try:
            self._navigate_within_ozon(url)
        except Exception as e:
            logger.warning("Ошибка загрузки страницы: %s", e)

        # --- Проверка на редирект на поиск ---
        current_url = self.driver.current_url
        if "/search/" in current_url and "/product/" not in current_url:
            logger.warning("Редирект на поиск в сессии: %s", url)
            raise AntibotBlockedError("Редирект на поиск (антибот)", marketplace="ozon")

        if self._is_blocked():
            logger.warning("Антибот в сессии — прерываю сессию")
            raise AntibotBlockedError(
                "Не удалось получить страницу: антибот/блокировка.",
                marketplace="ozon",
            )

        self._pause()
        self._random_mouse_movements()
        self._pause()
        try:
            self.wait.until(lambda d: "__NUXT__" in d.page_source)
            logger.info("Страница отдала __NUXT__ — данные получены")
            return self.driver.page_source
        except Exception:
            logger.warning("__NUXT__ не появился — считаю блокировкой")
            raise AntibotBlockedError(
                "Не удалось получить страницу: антибот/блокировка.",
                marketplace="ozon",
            )

    def screenshot_png(self) -> bytes | None:
        """Скриншот текущей страницы (для debug-артефактов). None при ошибке."""
        try:
            return self.driver.get_screenshot_as_png()
        except Exception as e:
            logger.debug("Скриншот не удался: %s", e)
            return None

    def close(self):
        """Закрыть драйвер. Постоянный профиль сохраняем (кука антибота),
        временный — удаляем."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.debug("Ошибка quit(): %s", e)
            self.driver = None
        if getattr(self, "_profile_is_temp", True) and self.chrome_temp_dir:
            shutil.rmtree(self.chrome_temp_dir, ignore_errors=True)
        logger.debug("OzonParser.close: ресурсы освобождены (temp=%s)",
                     getattr(self, "_profile_is_temp", True))

    def fetch_listing_html(self, url: str) -> str:
        """Открыть одну страницу выдачи, вернуть HTML. Без скролла — пагинация
        берётся из __NUXT__ (currentPage/totalPages), паджинатор в DOM не нужен."""
        self.driver.set_page_load_timeout(self.policy.page_load_timeout)
        for attempt in range(1, self.policy.retries + 1):
            logger.info("Открываю страницу выдачи (попытка %s/%s): %s",
                        attempt, self.policy.retries, url)
            self._pause()
            try:
                self.driver.get(url)
            except Exception as e:
                logger.warning("Ошибка загрузки выдачи: %s", e)
            if self._is_blocked():
                raise AntibotBlockedError("Выдача заблокирована антиботом.", marketplace="ozon")
            try:
                self.wait.until(lambda d: "tileGridDesktop" in d.page_source)
            except Exception:
                logger.warning("Плитки не появились на попытке %s", attempt)
            html = self.driver.page_source
            if "tileGridDesktop" in html:
                return html
            if attempt < self.policy.retries:
                time.sleep(random.uniform(self.policy.retry_pause_min,
                                          self.policy.retry_pause_max))
        logger.warning("Плитки не появились после всех попыток")
        return self.driver.page_source
