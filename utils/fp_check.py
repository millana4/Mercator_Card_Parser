# -*- coding: utf-8 -*-
"""
fp_check.py — проверка отпечатка браузера парсера без обращения к Ozon.

Поднимает локальный HTTP-сервер, открывает его в Chrome парсера (те же настройки,
что у воркера), собирает заголовки запроса и свойства браузера и сверяет их
с требованиями к согласованному профилю Windows и с профилем устройства.
Эталон марок Client Hints — чистый Chrome той же сборки без флагов и подмен.

Запуск в контейнере (Xvfb для эталонного Chrome поднимает entrypoint):
  docker compose run --rm -T --no-deps -e OZON_HEADLESS=false \
    -e OZON_CHROME_PROFILE_DIR= worker python -m utils.fp_check [поколение]
Код выхода 0 — все проверки пройдены, 1 — есть провалы.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from app.core.config import settings
from app.marketplaces.ozon.device_profile import DeviceProfile, pick_device
from app.marketplaces.ozon.parser import OzonParser

PORT = 8765
WINDOWS_FONTS = ["Arial", "Times New Roman", "Courier New", "Verdana",
                 "Segoe UI", "Calibri", "Cambria"]
# Шрифты, которых на Windows нет: Linux и замены под своими именами.
FOREIGN_FONTS = ["Liberation Sans", "Liberation Serif", "Liberation Mono", "DejaVu Sans",
                 "Arimo", "Tinos", "Cousine", "Carlito", "Caladea", "Selawik"]
EXPECTED_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
EXPECTED_LANGUAGES = ["ru-RU", "ru", "en-US", "en"]

headers: dict = {}

COLLECT_JS = r"""
(async () => {
  const r = {};
  const getterSource = (proto, prop) => {
    const d = Object.getOwnPropertyDescriptor(proto, prop);
    return d && d.get ? Function.prototype.toString.call(d.get) : null;
  };
  r.userAgent = navigator.userAgent;
  r.platform = navigator.platform;
  r.webdriver = navigator.webdriver;
  r.languages = navigator.languages;
  r.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  r.plugins = navigator.plugins.length;
  r.hardwareConcurrency = navigator.hardwareConcurrency;
  r.deviceMemory = navigator.deviceMemory;
  r.deviceMemoryGetter = getterSource(Navigator.prototype, 'deviceMemory');
  r.availHeightGetter = getterSource(Screen.prototype, 'availHeight');
  r.screen = [screen.width, screen.height, screen.availWidth, screen.availHeight];
  r.window = [innerWidth, innerHeight, outerWidth, outerHeight];
  r.uaData = null;
  if (navigator.userAgentData) {
    const he = await navigator.userAgentData.getHighEntropyValues(
      ['platformVersion', 'architecture', 'bitness', 'model', 'fullVersionList', 'wow64']);
    r.uaData = Object.assign({brands: navigator.userAgentData.brands,
      mobile: navigator.userAgentData.mobile,
      platform: navigator.userAgentData.platform}, he);
  }
  r.automationKeys = Object.keys(window).filter(k => /cdc_/.test(k));
  r.renamedKeys = Object.keys(window).filter(
    k => /^[a-z]{26}_(Array|Object|Promise|Proxy|Symbol|JSON|Window)$/.test(k));
  r.toStringSource = Function.prototype.toString.call(Function.prototype.toString);
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    r.webgl = gl ? {
      vendor: gl.getParameter(gl.VENDOR), renderer: gl.getParameter(gl.RENDERER),
      unmaskedVendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
      unmaskedRenderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
      getParameterSource: Function.prototype.toString.call(
        WebGLRenderingContext.prototype.getParameter)
    } : null;
  } catch (e) { r.webgl = null; r.webglError = String(e); }
  const ctx = document.createElement('canvas').getContext('2d');
  const width = f => { ctx.font = '72px ' + f; return ctx.measureText('mmmmmmmmmmlli').width; };
  const bases = ['monospace', 'serif', 'sans-serif'];
  const baseW = bases.map(width);
  const names = __FONT_NAMES__;
  r.fonts = Object.fromEntries(names.map(n =>
    [n, bases.some((b, i) => width('"' + n + '", ' + b) !== baseW[i])]));
  const c = document.createElement('canvas'); c.width = 300; c.height = 60;
  const x = c.getContext('2d');
  x.textBaseline = 'top'; x.font = '16px Arial'; x.fillStyle = '#f60';
  x.fillRect(10, 10, 100, 30); x.fillStyle = '#069'; x.fillText('Mercator fp 1.0', 4, 20);
  r.canvas = c.toDataURL();
  r.worker = await new Promise(res => {
    const src = 'postMessage({ua: navigator.userAgent, platform: navigator.platform, ' +
                'uaPlatform: navigator.userAgentData ? navigator.userAgentData.platform : null, ' +
                'cores: navigator.hardwareConcurrency, memory: navigator.deviceMemory})';
    const w = new Worker(URL.createObjectURL(new Blob([src], {type: 'text/javascript'})));
    w.onmessage = e => res(e.data);
    setTimeout(() => res(null), 3000);
  });
  return r;
})()
""".replace("__FONT_NAMES__", json.dumps(WINDOWS_FONTS + FOREIGN_FONTS))

REAL_BRANDS_JS = """
navigator.userAgentData.getHighEntropyValues(['fullVersionList'])
  .then(v => ({brands: v.brands, fullVersionList: v.fullVersionList}))
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            headers.update({k.lower(): v for k, v in self.headers.items()})
        body = b"<html><head><title>fp</title></head><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _evaluate(driver, expression: str):
    resp = driver.execute_cdp_cmd("Runtime.evaluate", {
        "expression": expression, "awaitPromise": True, "returnByValue": True})
    if "exceptionDetails" in resp:
        raise RuntimeError(json.dumps(resp["exceptionDetails"], ensure_ascii=False))
    return resp["result"]["value"]


def collect_parser(device: DeviceProfile) -> tuple[str, dict]:
    """Открыть локальную страницу в Chrome парсера и собрать свойства браузера."""
    parser = OzonParser(settings.ozon_policy(), device=device)
    try:
        full_version = parser.driver.capabilities.get("browserVersion", "")
        parser.driver.get(f"http://127.0.0.1:{PORT}/")
        # Как на страницах Ozon: после execute_script chromedriver вносит свои переменные.
        parser.driver.execute_script("return 1;")
        return full_version, _evaluate(parser.driver, COLLECT_JS)
    finally:
        parser.close()


def collect_real_brands() -> dict:
    """Настоящие марки Client Hints установленного Chrome: окно на Xvfb,
    без флагов профиля и подмен — эталон для сверки."""
    profile_dir = tempfile.mkdtemp()
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(f"--user-data-dir={profile_dir}")
    driver = webdriver.Chrome(service=Service("/usr/local/bin/chromedriver"), options=opts)
    try:
        driver.get(f"http://127.0.0.1:{PORT}/real")
        return _evaluate(driver, REAL_BRANDS_JS)
    finally:
        driver.quit()
        shutil.rmtree(profile_dir, ignore_errors=True)


def main() -> int:
    generation = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    device = pick_device(os.environ.get("PARSER_ID", "0"), generation)

    srv = HTTPServer(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        full_version, js = collect_parser(device)
        real = collect_real_brands()
    finally:
        srv.shutdown()

    major = full_version.split(".")[0]
    ua_data = js.get("uaData") or {}
    full_list = {b["brand"]: b["version"] for b in ua_data.get("fullVersionList") or []}
    fonts = js.get("fonts") or {}
    webgl = js.get("webgl") or {}
    scr, win = js["screen"], js["window"]
    expected_screen = [device.width, device.height, device.width, device.avail_height]

    checks = [
        ("User-Agent: Windows и версия браузера",
         "Windows NT 10.0" in js["userAgent"] and f"Chrome/{major}.0.0.0" in js["userAgent"],
         js["userAgent"]),
        ("заголовок User-Agent = navigator.userAgent",
         headers.get("user-agent") == js["userAgent"], headers.get("user-agent")),
        ("Sec-CH-UA с версией браузера",
         f'"Google Chrome";v="{major}"' in headers.get("sec-ch-ua", ""), headers.get("sec-ch-ua")),
        ("Sec-CH-UA-Platform = Windows",
         headers.get("sec-ch-ua-platform") == '"Windows"', headers.get("sec-ch-ua-platform")),
        ("Sec-CH-UA-Mobile = ?0",
         headers.get("sec-ch-ua-mobile") == "?0", headers.get("sec-ch-ua-mobile")),
        ("Accept-Language как у Chrome",
         headers.get("accept-language") == EXPECTED_ACCEPT_LANGUAGE, headers.get("accept-language")),
        ("navigator.languages", js["languages"] == EXPECTED_LANGUAGES, js["languages"]),
        ("navigator.platform = Win32", js["platform"] == "Win32", js["platform"]),
        ("userAgentData.platform = Windows",
         ua_data.get("platform") == "Windows", ua_data.get("platform")),
        ("platformVersion = 10.0.0 (Windows 10)",
         ua_data.get("platformVersion") == "10.0.0", ua_data.get("platformVersion")),
        ("architecture/bitness = x86/64",
         ua_data.get("architecture") == "x86" and ua_data.get("bitness") == "64",
         f'{ua_data.get("architecture")}/{ua_data.get("bitness")}'),
        ("brands = настоящие Chrome",
         ua_data.get("brands") == real.get("brands"),
         f'наши {ua_data.get("brands")}, настоящие {real.get("brands")}'),
        ("fullVersionList = настоящий Chrome",
         ua_data.get("fullVersionList") == real.get("fullVersionList"),
         f'наш {ua_data.get("fullVersionList")}, настоящий {real.get("fullVersionList")}'),
        ("fullVersionList: версия = браузер",
         full_list.get("Google Chrome") == full_version, full_list.get("Google Chrome")),
        ("navigator.webdriver = false", js["webdriver"] is False, js["webdriver"]),
        ("нет маркеров cdc_", not js["automationKeys"], js["automationKeys"]),
        ("WebGL есть", bool(webgl), webgl.get("renderer") if webgl else js.get("webglError")),
        ("WebGL: видеокарта устройства",
         webgl.get("unmaskedVendor") == device.webgl_vendor
         and webgl.get("unmaskedRenderer") == device.webgl_renderer,
         f'{webgl.get("unmaskedVendor")} / {webgl.get("unmaskedRenderer")}'),
        ("getParameter.toString как у встроенной",
         webgl.get("getParameterSource") == "function getParameter() { [native code] }",
         webgl.get("getParameterSource")),
        ("Function.prototype.toString как у встроенной",
         js["toStringSource"] == "function toString() { [native code] }", js["toStringSource"]),
        ("ядра = устройство", js["hardwareConcurrency"] == device.cores,
         f'{js["hardwareConcurrency"]} (устройство {device.cores})'),
        ("память = устройство", js["deviceMemory"] == device.memory,
         f'{js["deviceMemory"]} (устройство {device.memory})'),
        ("геттер deviceMemory как у встроенного",
         js["deviceMemoryGetter"] == "function get deviceMemory() { [native code] }",
         js["deviceMemoryGetter"]),
        ("геттер availHeight как у встроенного",
         js["availHeightGetter"] == "function get availHeight() { [native code] }",
         js["availHeightGetter"]),
        ("экран = устройство с панелью задач", scr == expected_screen,
         f"экран {scr}, ожидалось {expected_screen}"),
        ("окно в доступной области экрана", win[2] <= scr[2] and win[3] <= scr[3],
         f"окно {win}, доступно {scr[2]}x{scr[3]}"),
        ("шрифты Windows видны", all(fonts.get(n) for n in WINDOWS_FONTS),
         {n: fonts.get(n) for n in WINDOWS_FONTS}),
        ("шрифтов Linux и замен под своими именами нет",
         not any(fonts.get(n) for n in FOREIGN_FONTS),
         {n: fonts.get(n) for n in FOREIGN_FONTS}),
        ("часовой пояс Europe/Moscow", js["timezone"] == "Europe/Moscow", js["timezone"]),
        ("плагинов 5", js["plugins"] == 5, js["plugins"]),
    ]

    failed = 0
    for name, ok, value in checks:
        print(f"FP {'OK  ' if ok else 'FAIL'} {name}: {value}")
        failed += 0 if ok else 1

    print(f"FP INFO устройство: {device.name}, поколение {generation}, "
          f"PARSER_ID={os.environ.get('PARSER_ID', '0')}")
    print(f"FP INFO браузер: {full_version}")
    print(f"FP INFO переменные chromedriver под новым именем: {len(js['renamedKeys'])}")
    print(f"FP INFO canvas_sha: {hashlib.sha256(js['canvas'].encode()).hexdigest()[:12]}")
    print(f"FP INFO воркер: {js.get('worker')}")
    print(f"FP ИТОГ: провалов {failed} из {len(checks)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())