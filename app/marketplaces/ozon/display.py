# -*- coding: utf-8 -*-
"""
display.py — виртуальный экран Xvfb на одну сессию браузера.

Разрешение задаёт профиль устройства; дисплей — первый свободный начиная с :100
(:99 занят Xvfb из entrypoint.sh). Экран останавливается вместе с браузером.
"""
import os
import subprocess
import time

from app.core.logging import get_logger

logger = get_logger(__name__)

X11_DIR = "/tmp/.X11-unix"
LOCK_DIR = "/tmp"


def find_free_display(start: int = 100, end: int = 200,
                      x11_dir: str = X11_DIR, lock_dir: str = LOCK_DIR) -> int:
    """Первый номер дисплея без сокета X11 и без lock-файла."""
    for number in range(start, end):
        if (not os.path.exists(os.path.join(x11_dir, f"X{number}"))
                and not os.path.exists(os.path.join(lock_dir, f".X{number}-lock"))):
            return number
    raise RuntimeError(f"Нет свободного дисплея Xvfb в диапазоне :{start}–:{end - 1}")


class VirtualDisplay:
    """Процесс Xvfb с заданным разрешением."""

    def __init__(self, width: int, height: int, depth: int = 24):
        self.width = width
        self.height = height
        self.depth = depth
        self.number: int | None = None
        self._proc: subprocess.Popen | None = None

    @property
    def name(self) -> str:
        return f":{self.number}"

    def start(self, timeout: float = 10.0) -> None:
        """Запустить Xvfb и дождаться его сокета."""
        self.number = find_free_display()
        self._proc = subprocess.Popen(
            ["Xvfb", self.name, "-screen", "0",
             f"{self.width}x{self.height}x{self.depth}", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        socket_path = os.path.join(X11_DIR, f"X{self.number}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                self._proc = None
                raise RuntimeError(f"Xvfb {self.name} завершился с кодом {code}")
            if os.path.exists(socket_path):
                logger.info("Xvfb %s запущен: %sx%s", self.name, self.width, self.height)
                return
            time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"Xvfb {self.name} не поднялся за {timeout} с")

    def stop(self) -> None:
        """Остановить Xvfb (SIGTERM, при зависании — SIGKILL)."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception as e:
                logger.debug("Не удалось остановить Xvfb %s: %s", self.name, e)
        self._proc = None