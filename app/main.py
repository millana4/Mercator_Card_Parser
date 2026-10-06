# -*- coding: utf-8 -*-
"""
main.py — точка входа сервиса парсинга.

Сервис ТОЛЬКО парсит: на вход URL или артикул, на выход — распарсенные данные.
Формирование задач и запись в БД — в отдельных сервисах.

Порядок тегов в Swagger задаётся порядком подключения роутеров:
  Ozon → (в будущем WB, Я.Маркет) → Файлы (debug) → Служебное.
"""

import uvicorn
from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import setup_logging, get_logger
from app.core.exceptions import register_exception_handlers
from app.api.routes import ozon, wb, files, health

setup_logging()
logger = get_logger(__name__)

app = FastAPI(
    title="Parsing Service",
    description="Сервис парсинга карточек маркетплейсов. На вход URL или артикул — "
                "на выход распарсенные данные (очищенная карточка или сырой JSON). "
                "Сейчас поддержан Ozon; WB и Яндекс.Маркет добавляются как отдельные модули.",
    version="1.0.0",
)

register_exception_handlers(app)

# Сначала маркетплейсы (вверху Swagger), затем служебные разделы.
app.include_router(ozon.router, prefix=settings.api_prefix)
app.include_router(wb.router, prefix=settings.api_prefix)
app.include_router(files.router, prefix=settings.api_prefix)
app.include_router(health.router, prefix=settings.api_prefix)

logger.info("Parsing Service запущен. DEBUG=%s, префикс=%s", settings.debug, settings.api_prefix)


if __name__ == "__main__":
    # Локальный запуск по треугольнику в PyCharm.
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
