# -*- coding: utf-8 -*-
"""
parser.py — низкоуровневый движок парсинга Ozon.

Ответственность: открыть страницу в Chrome с согласованным профилем Windows, дождаться данных, вернуть
HTML и извлечённый объект __NUXT__.state. Ничего не сохраняет на диск и не
собирает доменную карточку — это делают слои выше (repository/service/mapper).

Таймауты, паузы и ретраи берутся из MarketplacePolicy (см. core/config.py),
поэтому у Ozon своя политика, а у будущих WB/Я.Маркет — своя.

Устройство (экран, ядра, память, видеокарта) — из профиля устройства
(device_profile.py); экран — свой Xvfb на сессию браузера (display.py).
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
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from app.core.config import MarketplacePolicy
from app.core.logging import get_logger
from app.core.exceptions import AntibotBlockedError, OutOfStockError, PageLoadError
from app.marketplaces.ozon.device_profile import DeviceProfile, pick_device
from app.marketplaces.ozon.display import VirtualDisplay
from app.marketplaces.ozon.nuxt import extract_nuxt_state

logger = get_logger(__name__)

# Маркер полного блока характеристик. Виджет приходит второй порцией страницы
# (paginator, fetchType=scroll) — когда окно прокручено к концу загруженной части.
FULL_CONTENT_MARKER = 'data-widget="webCharacteristics"'

# --- Согласованный профиль Windows ---
# User-Agent Chrome для Windows 10 x64; версия — мажорная установленного Chrome
# (Chrome сам сокращает её до «NNN.0.0.0»).
WINDOWS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
# Список языков без весов: Chrome сам расставит q в заголовке Accept-Language.
ACCEPT_LANGUAGE = "ru-RU,ru,en-US,en"

# Подмены устройства: выполняются до скриптов каждой новой страницы.
# Строки видеокарты WebGL (CFG.webglSpoof), navigator.deviceMemory, screen.availHeight.
# При CFG.toStringProxy подменённые функции и геттеры при toString() показывают
# текст оригинала, как встроенные («function getParameter() { [native code] }»).
_DEVICE_PATCH_JS = """
(() => {
  const CFG = __CFG__;
  const originals = new WeakMap();
  if (CFG.toStringProxy) {
    const nativeToString = Function.prototype.toString;
    const toStringProxy = new Proxy(nativeToString, {
      apply(target, self, args) {
        return Reflect.apply(target, originals.get(self) || self, args);
      },
    });
    originals.set(toStringProxy, nativeToString);
    Function.prototype.toString = toStringProxy;
  }

  const wrap = (fn, apply) => {
    const proxy = new Proxy(fn, {apply});
    originals.set(proxy, fn);
    return proxy;
  };
  const patchGetter = (proto, prop, value) => {
    const d = proto && Object.getOwnPropertyDescriptor(proto, prop);
    if (!d || !d.get) return;
    Object.defineProperty(proto, prop, Object.assign({}, d, {get: wrap(d.get, () => value)}));
  };
  const patchWebGL = (proto) => {
    if (!proto) return;
    proto.getParameter = wrap(proto.getParameter, (target, self, args) => {
      if (args[0] === 0x9245) return CFG.webglVendor;
      if (args[0] === 0x9246) return CFG.webglRenderer;
      return Reflect.apply(target, self, args);
    });
  };
  if (CFG.webglSpoof) {
    patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  }
  patchGetter(window.Navigator && Navigator.prototype, 'deviceMemory', CFG.deviceMemory);
  patchGetter(window.Screen && Screen.prototype, 'availHeight', CFG.availHeight);
})();
"""

# Марки Client Hints — как их строит Chromium (components/embedder_support/
# user_agent_utils.cc): символы и версия GREASE-марки и порядок трёх марок
# выбираются по мажорной версии.
_GREASE_CHARS = [" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_"]
_GREASE_VERSIONS = ["8", "99", "24"]
_BRAND_ORDERS = [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)]


def chrome_brands(full_version: str) -> tuple[list[dict], list[dict]]:
    """brands и fullVersionList Client Hints Google Chrome указанной версии.
    Флаг --user-agent обнуляет настоящие Client Hints браузера, поэтому строим
    их сами; совпадение с настоящими проверяет utils/fp_check.py."""
    major = int(full_version.split(".")[0])
    grease_brand = f"Not{_GREASE_CHARS[major % 11]}A{_GREASE_CHARS[(major + 1) % 11]}Brand"
    grease_version = _GREASE_VERSIONS[major % 3]
    entries = [(grease_brand, grease_version, f"{grease_version}.0.0.0"),
               ("Chromium", str(major), full_version),
               ("Google Chrome", str(major), full_version)]
    brands: list = [None, None, None]
    full_list: list = [None, None, None]
    for slot, (brand, version, full) in zip(_BRAND_ORDERS[major % 6], entries):
        brands[slot] = {"brand": brand, "version": version}
        full_list[slot] = {"brand": brand, "version": full}
    return brands, full_list




# --- Переключатели подмен отпечатка (OZON_FP_*, см. config.py) ---
# WebGL: spoof — программный WebGL (SwiftShader) и строка видеокарты из профиля
# устройства; native — программный WebGL без подмены строки (видна SwiftShader);
# off — WebGL выключен, как в коде до 13.10 (--disable-gpu, без SwiftShader).


def chrome_gl_args(webgl_mode: str) -> list[str]:
    """Флаги Chrome для режима WebGL."""
    if webgl_mode == "off":
        return ["--disable-gpu"]
    # Программный WebGL: GPU на сервере нет, без этих флагов WebGL недоступен.
    return ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]


def device_patch_js(device: DeviceProfile, webgl_mode: str, tostring_proxy: bool) -> str:
    """Скрипт подмен устройства для Page.addScriptToEvaluateOnNewDocument."""
    cfg = {
        "webglSpoof": webgl_mode == "spoof",
        "toStringProxy": bool(tostring_proxy),
        "webglVendor": device.webgl_vendor,
        "webglRenderer": device.webgl_renderer,
        "deviceMemory": device.memory,
        "availHeight": device.avail_height,
    }
    return _DEVICE_PATCH_JS.replace("__CFG__", json.dumps(cfg, ensure_ascii=False))


def ua_override(full_version: str, client_hints: bool) -> dict:
    """Параметры Emulation.setUserAgentOverride: User-Agent и navigator.platform
    Windows 10 x64; с client_hints — ещё userAgentMetadata (марки по алгоритму
    Chromium, Windows 10, x86/64). Без client_hints Client Hints не подменяются."""
    major = full_version.split(".")[0]
    override = {
        "userAgent": WINDOWS_UA.format(major=major),
        "acceptLanguage": ACCEPT_LANGUAGE,
        "platform": "Win32",
    }
    if client_hints:
        brands, full_list = chrome_brands(full_version)
        override["userAgentMetadata"] = {
            "brands": brands,
            "fullVersionList": full_list,
            "fullVersion": full_version,
            "platform": "Windows",
            "platformVersion": "10.0.0",
            "architecture": "x86",
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": False,
        }
    return override


class OzonParser:
    """Драйвер + получение HTML + извлечение __NUXT__ для одной карточки Ozon."""

    def __init__(self, policy: MarketplacePolicy, device: DeviceProfile | None = None,
                 profile_dir: str | None = None):
        self.policy = policy
        self.device = device or pick_device(os.environ.get("PARSER_ID", "0"), 0)
        self.driver = None
        self.wait = None
        self.display: VirtualDisplay | None = None
        # Постоянный профиль (том): кука-пропуск антибота переживает карточки и
        # рестарты. profile_dir задаёт сервис для своей сессии ("" — временный
        # профиль); None — как раньше, из OZON_CHROME_PROFILE_DIR.
        persistent = os.environ.get("OZON_CHROME_PROFILE_DIR") if profile_dir is None else profile_dir
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
            if not self.policy.headless:
                # Свой экран с разрешением устройства на время жизни браузера.
                self.display = VirtualDisplay(self.device.width, self.device.height)
                self.display.start()
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
        """Chrome с согласованным профилем Windows (без selenium-stealth):
        окно на своём Xvfb (OZON_HEADLESS=false), программный WebGL, шрифты
        по умолчанию как в Chrome на Windows, User-Agent и Client Hints —
        Windows 10 с версией установленного Chrome, устройство — из профиля."""
        logger.debug("_setup_driver: headless=%s, устройство=%s",
                     self.policy.headless, self.device.name)
        major = (os.environ.get("CHROME_VERSION") or "").split(".")[0]
        opts = Options()
        if self.policy.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-setuid-sandbox")
        # Окно на всю доступную область экрана (без панели задач).
        opts.add_argument(f"--window-size={self.device.width},{self.device.avail_height}")
        opts.add_argument("--window-position=0,0")
        opts.add_argument("--lang=ru-RU")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        for arg in chrome_gl_args(self.policy.fp_webgl):
            opts.add_argument(arg)
        if major:
            # UA для всех запросов браузера, включая воркеры; Client Hints и platform — через CDP.
            opts.add_argument(f"--user-agent={WINDOWS_UA.format(major=major)}")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_experimental_option("prefs", {
            "intl.accept_languages": "ru-RU,ru,en-US,en",
            "webkit.webprefs.fonts.standard.Zyyy": "Times New Roman",
            "webkit.webprefs.fonts.serif.Zyyy": "Times New Roman",
            "webkit.webprefs.fonts.sansserif.Zyyy": "Arial",
            "webkit.webprefs.fonts.fixed.Zyyy": "Courier New",
        })
        opts.add_argument(f"--user-data-dir={self.chrome_temp_dir}")

        env = dict(os.environ)
        if self.display is not None:
            env["DISPLAY"] = self.display.name
        service = Service("/usr/local/bin/chromedriver", env=env)
        self.driver = webdriver.Chrome(service=service, options=opts)
        self._apply_windows_profile()
        logger.info("Драйвер Chrome инициализирован (headless=%s, устройство=%s, %sx%s; "
                    "подмены: WebGL=%s, toString=%s, Client Hints=%s)",
                    self.policy.headless, self.device.name,
                    self.device.width, self.device.height, self.policy.fp_webgl,
                    "proxy" if self.policy.fp_tostring_proxy else "нет",
                    "да" if self.policy.fp_client_hints else "нет")

    def _apply_windows_profile(self) -> None:
        """Подмены через CDP: User-Agent и navigator.platform — Windows 10 x64
        с версией установленного Chrome; Client Hints — при OZON_FP_CLIENT_HINTS;
        число ядер, память, доступная высота экрана — из профиля устройства;
        видеокарта WebGL — при OZON_FP_WEBGL=spoof; Proxy на toString —
        при OZON_FP_TOSTRING_PROXY. Марки Client Hints — по алгоритму Chromium."""
        full_version = self.driver.capabilities.get("browserVersion", "")
        major = full_version.split(".")[0]
        env_major = (os.environ.get("CHROME_VERSION") or "").split(".")[0]
        if env_major and env_major != major:
            logger.warning("CHROME_VERSION=%s не совпадает с версией браузера %s",
                           env_major, full_version)

        self.driver.execute_cdp_cmd("Emulation.setUserAgentOverride",
                                    ua_override(full_version, self.policy.fp_client_hints))
        self.driver.execute_cdp_cmd("Emulation.setHardwareConcurrencyOverride",
                                    {"hardwareConcurrency": self.device.cores})
        patch_js = device_patch_js(self.device, self.policy.fp_webgl,
                                   self.policy.fp_tostring_proxy)
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": patch_js})

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

        # 1. Быстрая проверка по заголовку. __rr=1 в адресе ставится и на обычную
        #    страницу после прохождения проверки защиты (заголовок при этом Ozon),
        #    поэтому сам по себе он признаком блокировки не считается.
        if "Antibot Captcha" in title or ("__rr=1" in cur_url and "Ozon" not in title):
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


    def ensure_full_content(self) -> bool:
        """
        Дождаться полного блока характеристик (webCharacteristics).
        Блок приходит второй порцией страницы при прокрутке к её концу. Если он
        уже в разметке — выходим сразу, без прокрутки. Иначе прокручиваем вниз
        с паузами, пока блок не появится или не выйдет policy.full_content_timeout.
        На странице удалённого товара не ждём. Исключений не бросает.
        Возвращает True, если блок в разметке есть.
        """
        timeout = self.policy.full_content_timeout
        deadline = time.monotonic() + timeout
        scrolls = 0
        while True:
            try:
                src = self.driver.page_source
            except Exception as e:
                logger.warning("Не удалось прочитать страницу при ожидании полного блока: %s", e)
                return False
            if FULL_CONTENT_MARKER in src:
                logger.info("Полный блок характеристик на странице (прокруток %s)", scrolls)
                return True
            if 'data-widget="error"' in src or "Такой страницы не существует" in src:
                return False
            if time.monotonic() >= deadline:
                break
            try:
                self.driver.execute_script(f"window.scrollBy(0, {random.randint(800, 1400)});")
            except Exception as e:
                logger.warning("Прокрутка при ожидании полного блока не удалась: %s", e)
                return False
            scrolls += 1
            time.sleep(random.uniform(1.0, 2.0))
        logger.warning("Полный блок характеристик не появился за %s с (прокруток %s)",
                       timeout, scrolls)
        return False

    def fetch_html(self, url: str, full_content: bool = False) -> str:
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
                sku = self._sku_from_url(url)
                if sku and f"product_id={sku}" in current_url:
                    logger.info("Товар закончился (редирект на поиск, product_id=%s)", sku)
                    raise OutOfStockError(f"Товар закончился: sku={sku}")
                logger.warning("Редирект на поиск вместо карточки (антибот): %s", url)
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
                if full_content:
                    self.ensure_full_content()
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

    def fetch_html_in_session(self, url: str, full_content: bool = False) -> str:
        """Открыть карточку в УЖЕ живом браузере (сессия переиспользуется).
        Капча или блок → AntibotBlockedError (бан). Браузер не ответил или нет
        __NUXT__ без признаков капчи → PageLoadError (сбой загрузки)."""
        self.driver.set_page_load_timeout(self.policy.page_load_timeout)
        logger.info("Открываю карточку в сессии: %s", url)
        self._pause()
        try:
            self._navigate_within_ozon(url)
        except Exception as e:
            logger.warning("Ошибка загрузки страницы: %s", e)

        try:
            current_url = self.driver.current_url
        except Exception as e:
            logger.warning("Браузер не отвечает после перехода: %s", e)
            raise PageLoadError("Браузер не отвечает после перехода.", marketplace="ozon")

        # --- Проверка на редирект на поиск ---
        if "/search/" in current_url and "/product/" not in current_url:
            sku = self._sku_from_url(url)
            if sku and f"product_id={sku}" in current_url:
                logger.info("Товар закончился (редирект на поиск, product_id=%s)", sku)
                raise OutOfStockError(f"Товар закончился: sku={sku}")
            logger.warning("Редирект на поиск в сессии (антибот): %s", url)
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
        except Exception:
            if self._is_blocked():
                raise AntibotBlockedError(
                    "Не удалось получить страницу: антибот/блокировка.",
                    marketplace="ozon",
                )
            logger.warning("__NUXT__ не появился за %s с, признаков капчи нет — сбой загрузки",
                           self.policy.wait_timeout)
            raise PageLoadError("Страница не загрузилась: нет __NUXT__, признаков капчи нет.",
                                marketplace="ozon")
        logger.info("Страница отдала __NUXT__ — данные получены")
        if full_content:
            self.ensure_full_content()
        return self.driver.page_source

    def screenshot_png(self) -> bytes | None:
        """Скриншот текущей страницы (для debug-артефактов). None при ошибке."""
        try:
            return self.driver.get_screenshot_as_png()
        except Exception as e:
            logger.debug("Скриншот не удался: %s", e)
            return None

    def block_snapshot(self) -> dict:
        """title, адрес и скриншот текущей страницы — для разбора блокировки.
        Исключений не бросает: что не удалось получить, будет None."""
        title = url = None
        try:
            title = self.driver.title
            url = self.driver.current_url
        except Exception as e:
            logger.debug("Не удалось прочитать title/адрес при блокировке: %s", e)
        return {"title": title, "url": url, "png": self.screenshot_png()}

    def close(self):
        """Закрыть драйвер и свой Xvfb. Постоянный профиль сохраняем
        (кука антибота), временный — удаляем."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.debug("Ошибка quit(): %s", e)
            self.driver = None
        if self.display is not None:
            self.display.stop()
            self.display = None
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


    def fetch_listing_scrolled(self, url: str, scroll_steps: int) -> str:
        """Выдача как у живого покупателя: (главная, если браузер не на Ozon) ->
        переход на поиск изнутри Ozon (браузер ставит Referer) -> ожидание новой
        страницы и плиток -> прокрутка scroll_steps раз, Ozon догружает плитки.
        В карточки не заходим. Возвращает HTML страницы."""
        self.driver.set_page_load_timeout(self.policy.page_load_timeout)
        try:
            cur = self.driver.current_url or ""
        except Exception:
            cur = ""
        if "ozon.ru" not in cur:
            self.visit_home()

        logger.info("Выдача с прокруткой (шагов %s): %s", scroll_steps, url)
        old_root = self.driver.find_element(By.TAG_NAME, "html")
        self.driver.execute_script("window.location.assign(arguments[0]);", url)
        try:
            WebDriverWait(self.driver, self.policy.page_load_timeout).until(ec.staleness_of(old_root))
        except Exception:
            logger.warning("Переход на выдачу не подтвердился за %s сек", self.policy.page_load_timeout)

        if self._is_blocked():
            raise AntibotBlockedError("Выдача заблокирована антиботом.", marketplace="ozon")
        try:
            self.wait.until(lambda d: "tileGridDesktop" in d.page_source)
        except Exception:
            logger.warning("Плитки не появились за %s сек", self.policy.wait_timeout)
            if self._is_blocked():
                raise AntibotBlockedError("Выдача заблокирована антиботом.", marketplace="ozon")

        self._pause()
        for step in range(scroll_steps):
            self.driver.execute_script(f"window.scrollBy(0, {random.randint(1400, 2000)});")
            time.sleep(random.uniform(2.0, 4.0))
            logger.debug("Прокрутка выдачи: шаг %s/%s", step + 1, scroll_steps)
        return self.driver.page_source


    # Ссылка и текстовые фрагменты каждой отрисованной плитки, в порядке на странице
    _TILES_JS = """
        return Array.from(document.querySelectorAll('.tile-root')).map(function (el) {
            var a = el.querySelector('a[href*="/product/"]');
            var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            var texts = [], node;
            while ((node = walker.nextNode())) {
                var s = node.nodeValue.trim();
                if (s) texts.push(s);
            }
            return {href: a ? a.getAttribute('href') : null, texts: texts};
        });
    """

    def collect_tiles(self) -> list[dict]:
        """Отрисованные плитки выдачи: [{href, texts}]."""
        try:
            return self.driver.execute_script(self._TILES_JS) or []
        except Exception as e:
            logger.warning("Не удалось собрать плитки со страницы: %s", e)
            return []

    def scroll_collect_tiles(self, target: int, max_scrolls: int) -> tuple[list[dict], int]:
        """Прокручивать выдачу, пока плиток меньше target, не больше max_scrolls раз;
        2 прокрутки подряд без новых плиток - выдача кончилась.
        Возвращает (плитки, сделано прокруток)."""
        tiles = self.collect_tiles()
        scrolls = stale = 0
        while len(tiles) < target and scrolls < max_scrolls and stale < 2:
            self.driver.execute_script(f"window.scrollBy(0, {random.randint(1400, 2000)});")
            time.sleep(random.uniform(2.0, 4.0))
            scrolls += 1
            fresh = self.collect_tiles()
            stale = stale + 1 if len(fresh) <= len(tiles) else 0
            tiles = fresh
        logger.info("Плиток на странице %s (нужно %s), прокруток %s", len(tiles), target, scrolls)
        return tiles, scrolls


    @staticmethod
    def _sku_from_url(url: str) -> str | None:
        """sku из url карточки: .../product/<sku>/ или .../product/<slug>-<sku>/."""
        import re
        m = re.search(r"/product/(?:[^/]*-)?(\d+)", url)
        return m.group(1) if m else None