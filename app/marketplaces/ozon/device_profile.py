# -*- coding: utf-8 -*-
"""
device_profile.py — наборы устройств для согласованного профиля Windows.

Набор — целое устройство: экран, ядра, память, видеокарта (в формате Chrome
на Windows: ANGLE/Direct3D11 с кодом устройства). Набор выбирается по номеру
парсера и «поколению»: поколение растёт при каждом бане, и вместе со сбросом
профиля браузера парсер получает следующее устройство.
"""
import hashlib
from dataclasses import dataclass

TASKBAR_HEIGHT = 40   # панель задач Windows внизу экрана, пикселей


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    width: int
    height: int
    cores: int              # navigator.hardwareConcurrency (логические ядра)
    memory: int             # navigator.deviceMemory, ГБ
    webgl_vendor: str
    webgl_renderer: str

    @property
    def avail_height(self) -> int:
        """Высота экрана без панели задач — как screen.availHeight на Windows."""
        return self.height - TASKBAR_HEIGHT


def _angle(vendor: str, gpu: str, device_id: str) -> str:
    return f"ANGLE ({vendor}, {gpu} ({device_id}) Direct3D11 vs_5_0 ps_5_0, D3D11)"


DEVICE_PROFILES = [
    DeviceProfile("ноутбук HD 520", 1366, 768, 4, 8, "Google Inc. (Intel)",
                  _angle("Intel", "Intel(R) HD Graphics 520", "0x00001916")),
    DeviceProfile("ноутбук UHD 620", 1920, 1080, 8, 8, "Google Inc. (Intel)",
                  _angle("Intel", "Intel(R) UHD Graphics 620", "0x00005917")),
    DeviceProfile("ноутбук Iris Xe", 1920, 1080, 8, 16, "Google Inc. (Intel)",
                  _angle("Intel", "Intel(R) Iris(R) Xe Graphics", "0x00009A49")),
    DeviceProfile("ноутбук Radeon", 1920, 1080, 12, 16, "Google Inc. (AMD)",
                  _angle("AMD", "AMD Radeon(TM) Graphics", "0x00001636")),
    DeviceProfile("ПК UHD 630", 1920, 1080, 6, 8, "Google Inc. (Intel)",
                  _angle("Intel", "Intel(R) UHD Graphics 630", "0x00003E92")),
    DeviceProfile("ПК GTX 1050 Ti", 1600, 900, 4, 8, "Google Inc. (NVIDIA)",
                  _angle("NVIDIA", "NVIDIA GeForce GTX 1050 Ti", "0x00001C82")),
    DeviceProfile("ПК GTX 1650", 1920, 1080, 12, 16, "Google Inc. (NVIDIA)",
                  _angle("NVIDIA", "NVIDIA GeForce GTX 1650", "0x00001F82")),
    DeviceProfile("ПК RTX 3060", 2560, 1440, 16, 16, "Google Inc. (NVIDIA)",
                  _angle("NVIDIA", "NVIDIA GeForce RTX 3060", "0x00002503")),
]


def pick_device(parser_id: str, generation: int = 0) -> DeviceProfile:
    """Набор устройства для парсера и поколения. Числовой номер парсера сдвигает
    стартовый набор (соседние парсеры — разные устройства), иначе — хэш номера."""
    pid = str(parser_id).strip()
    base = int(pid) if pid.isdigit() else int(hashlib.sha256(pid.encode()).hexdigest(), 16)
    return DEVICE_PROFILES[(base + max(generation, 0)) % len(DEVICE_PROFILES)]