# -*- coding: utf-8 -*-
"""
exceptions.py — доменные исключения сервиса и их обработчики.

Зачем отдельные типы: вызывающим сервисам (таск-менеджеру) важно различать
ситуации, чтобы решать, что делать дальше:
  - AntibotBlockedError   — заблокировал антибот → имеет смысл повторить позже;
  - PageLoadError         — страница не загрузилась, признаков капчи нет (подтип
                            AntibotBlockedError: для ядра тот же код antibot_blocked);
  - ProductNotFoundError  — товара/данных нет → пропустить;
  - InvalidRequestError   — кривой URL/SKU → отклонить, не повторять;
  - OutOfStockError       — товар закончился: Ozon увёл на поиск, сохранив product_id запрошенного sku
  - ParsingError          — прочая ошибка парсинга.

Каждый тип маппится на свой HTTP-статус и единый формат тела ошибки.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class ParsingError(Exception):
    """Базовая ошибка парсинга."""
    http_status = 502
    code = "parsing_error"

    def __init__(self, message: str, marketplace: str | None = None):
        super().__init__(message)
        self.message = message
        self.marketplace = marketplace


class AntibotBlockedError(ParsingError):
    """Антибот отдал капчу/блок. Имеет смысл повторить позже."""
    http_status = 503
    code = "antibot_blocked"


class PageLoadError(AntibotBlockedError):
    """Страница не загрузилась: браузер не ответил или нет __NUXT__ при отсутствии
    признаков капчи. Для ядра — то же «повторить позже» (код antibot_blocked,
    обработка в ядре не меняется). Воркер считает это сбоем загрузки, а не баном:
    пересоздаёт браузер; баном считается только серия сбоев подряд."""


class ProductNotFoundError(ParsingError):
    """Данные товара не найдены на странице."""
    http_status = 404
    code = "product_not_found"


class InvalidRequestError(ParsingError):
    """Некорректный вход (URL/SKU). Повторять бессмысленно."""
    http_status = 400
    code = "invalid_request"


class OutOfStockError(Exception):
    """Товар закончился: Ozon увёл на поиск, сохранив product_id нашего sku.
    Это НЕ антибот — карточка помечается out_of_stock, сессия не рвётся."""
    def __init__(self, message: str = "Товар закончился", marketplace: str = "ozon"):
        super().__init__(message)
        self.marketplace = marketplace


def _error_body(exc: ParsingError) -> dict:
    return {
        "ok": False,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "marketplace": exc.marketplace,
        },
    }


async def parsing_error_handler(request: Request, exc: ParsingError) -> JSONResponse:
    """Единый обработчик доменных ошибок парсинга."""
    return JSONResponse(status_code=exc.http_status, content=_error_body(exc))


def register_exception_handlers(app) -> None:
    """Подключить обработчики ко всем подтипам ParsingError."""
    for exc_type in (ParsingError, AntibotBlockedError,
                     ProductNotFoundError, InvalidRequestError):
        app.add_exception_handler(exc_type, parsing_error_handler)
