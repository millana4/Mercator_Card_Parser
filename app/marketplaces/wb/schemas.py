# -*- coding: utf-8 -*-
"""schemas.py — схемы API Wildberries (вход/выход эндпоинтов)."""
from pydantic import BaseModel, Field


class WbRawByUrlRequest(BaseModel):
    url: str = Field(
        ...,
        description="URL страницы Wildberries: поиск или карточка товара",
        examples=["https://www.wildberries.ru/catalog/0/search.aspx?search=лонгслив для девочки"],
    )
    extra_wait_s: float = Field(
        5.0, ge=0, le=60,
        description="Дополнительное ожидание после загрузки, сек: WB дорисовывает страницу скриптами",
    )


class WbRawResponse(BaseModel):
    """Ответ разведки: куда в итоге привёл браузер и где сохранены HTML и скриншот."""
    ok: bool = True
    url: str
    final_url: str | None = None
    title: str | None = None
    html_length: int
    debug_files: dict[str, str] = Field(default_factory=dict,
                                        description="Имена файлов в debug-каталоге (/files)")