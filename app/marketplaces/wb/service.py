# -*- coding: utf-8 -*-
"""
service.py — бизнес-логика Wildberries (оркестратор).

W0: только получение сырой страницы для разведки. Карточка, подбор и
долгоживущая сессия обхода добавляются на этапах W2–W4.
"""
import os
from datetime import datetime
from urllib.parse import urlparse

from app.core.config import MarketplacePolicy, settings
from app.core.exceptions import InvalidRequestError
from app.core.logging import get_logger
from app.marketplaces.wb.parser import WbParser

logger = get_logger(__name__)

WB_DOMAIN = "wildberries.ru"


class WbService:
    def __init__(self, policy: MarketplacePolicy | None = None):
        self.policy = policy or settings.wb_policy()

    @staticmethod
    def _check_url(url: str) -> str:
        url = (url or "").strip()
        host = (urlparse(url).hostname or "").lower()
        if not (host == WB_DOMAIN or host.endswith("." + WB_DOMAIN)):
            raise InvalidRequestError(f"Ссылка не на Wildberries: {url!r}", marketplace="wb")
        return url

    def get_raw_page(self, url: str, extra_wait_s: float) -> dict:
        """Открыть страницу WB в новом браузере, сохранить HTML и скриншот."""
        url = self._check_url(url)
        parser = WbParser(self.policy)
        try:
            page = parser.open_page(url, extra_wait_s)
        finally:
            parser.close()
        files = self._save(page)
        logger.info("WB raw: url=%s → final_url=%s, title=%r, html=%s симв., файлы=%s",
                    url, page["final_url"], page["title"], len(page["html"]), files)
        return {
            "url": url,
            "final_url": page["final_url"],
            "title": page["title"],
            "html_length": len(page["html"]),
            "debug_files": files,
        }

    @staticmethod
    def _save(page: dict) -> dict[str, str]:
        """Сохранить HTML и скриншот в debug-каталог (всегда: это разведка)."""
        os.makedirs(settings.debug_dir, exist_ok=True)
        base = f"wb_{datetime.now():%Y%m%d_%H%M%S}"
        files: dict[str, str] = {}
        html_path = os.path.join(settings.debug_dir, f"{base}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page["html"])
        files["html"] = os.path.basename(html_path)
        if page.get("screenshot_png"):
            png_path = os.path.join(settings.debug_dir, f"{base}.png")
            with open(png_path, "wb") as f:
                f.write(page["screenshot_png"])
            files["png"] = os.path.basename(png_path)
        return files