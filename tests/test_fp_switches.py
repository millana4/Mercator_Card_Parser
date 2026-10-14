# -*- coding: utf-8 -*-
"""Переключатели подмен отпечатка: флаги Chrome, скрипт подмен, Client Hints, разбор .env."""
import json

from app.core.config import settings
from app.marketplaces.ozon.device_profile import pick_device
from app.marketplaces.ozon.parser import (chrome_gl_args, device_patch_js,
                                          ua_override, WINDOWS_UA)

VERSION = "152.0.7977.64"
SWIFTSHADER = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]


def _cfg(js: str) -> dict:
    """Конфиг подмен, вставленный в скрипт (const CFG = {...};)."""
    start = js.index("const CFG = ") + len("const CFG = ")
    return json.loads(js[start:js.index(";\n", start)])


def test_gl_args_by_mode():
    assert chrome_gl_args("spoof") == SWIFTSHADER
    assert chrome_gl_args("native") == SWIFTSHADER
    assert chrome_gl_args("off") == ["--disable-gpu"]


def test_patch_js_flags():
    device = pick_device("14", 0)
    cfg = _cfg(device_patch_js(device, "spoof", True))
    assert cfg["webglSpoof"] is True and cfg["toStringProxy"] is True
    assert cfg["webglRenderer"] == device.webgl_renderer
    assert cfg["deviceMemory"] == device.memory and cfg["availHeight"] == device.avail_height
    assert _cfg(device_patch_js(device, "native", True))["webglSpoof"] is False
    assert _cfg(device_patch_js(device, "spoof", False))["toStringProxy"] is False
    off = _cfg(device_patch_js(device, "off", False))
    assert off["webglSpoof"] is False and off["toStringProxy"] is False
    # подмены памяти и экрана от переключателей не зависят
    assert (off["deviceMemory"], off["availHeight"]) == (cfg["deviceMemory"], cfg["availHeight"])


def test_ua_override_with_and_without_client_hints():
    full = ua_override(VERSION, True)
    assert full["userAgent"] == WINDOWS_UA.format(major="152")
    assert full["platform"] == "Win32"
    meta = full["userAgentMetadata"]
    assert (meta["platform"], meta["platformVersion"], meta["bitness"]) == ("Windows", "10.0.0", "64")
    assert meta["fullVersion"] == VERSION
    bare = ua_override(VERSION, False)
    assert "userAgentMetadata" not in bare
    assert bare["platform"] == "Win32" and bare["userAgent"] == full["userAgent"]


def test_policy_defaults(monkeypatch):
    for key in ("OZON_FP_WEBGL", "OZON_FP_TOSTRING_PROXY", "OZON_FP_CLIENT_HINTS"):
        monkeypatch.delenv(key, raising=False)
    p = settings.ozon_policy()
    assert (p.fp_webgl, p.fp_tostring_proxy, p.fp_client_hints) == ("spoof", True, True)


def test_policy_from_env(monkeypatch):
    monkeypatch.setenv("OZON_FP_WEBGL", "OFF")
    monkeypatch.setenv("OZON_FP_TOSTRING_PROXY", "false")
    monkeypatch.setenv("OZON_FP_CLIENT_HINTS", "0")
    p = settings.ozon_policy()
    assert (p.fp_webgl, p.fp_tostring_proxy, p.fp_client_hints) == ("off", False, False)


def test_unknown_webgl_mode_falls_back(monkeypatch):
    monkeypatch.setenv("OZON_FP_WEBGL", "gpu")
    assert settings.ozon_policy().fp_webgl == "spoof"