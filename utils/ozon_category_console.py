#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Достаёт количество товаров из window.__NUXT__. Вход — файл nuxt.txt рядом со скриптом."""
import re
import sys
from pathlib import Path

INPUT_FILE = "nuxt.txt"

def normalize(raw: str) -> str:
    text = raw.replace("\\\\", "\\")
    text = text.replace('\\"', '"')
    text = text.replace("\\u002F", "/").replace("\\u002f", "/")
    return text

PATTERNS = {
    "категория_id":        r'"categoryId"\s*:\s*(\d+)',
    "категория":           r'"h1"\s*:\s*"([^"]+)"',
    "товаров_в_категории": r'"offerCount"\s*:\s*(\d+)',
    "отзывов":             r'"reviewCount"\s*:\s*(\d+)',
    "рейтинг":             r'"ratingValue"\s*:\s*"?([\d.]+)',
    "мин_цена":            r'"lowPrice"\s*:\s*"?([\d.]+)',
    "страниц_выдачи":      r'"totalPages"\s*:\s*(\d+)',
    "url_страницы":        r'"canonical"[^}]*|"pageUrl"\s*:\s*"([^"]+)"',
}

def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / INPUT_FILE
    if not path.exists():
        print(f"Файл не найден: {path}")
        return

    raw = path.read_text(encoding="utf-8", errors="replace")
    text = normalize(raw)
    print(f"[файл: {path.name}, {len(raw)} символов]")

    # диагностика склейки: сколько РАЗНЫХ значений ключевых полей в файле
    for key in ("categoryId", "offerCount", "h1"):
        found = re.findall(rf'"{key}"\s*:\s*"?([^",}}]+)', text)
        uniq = list(dict.fromkeys(found))
        if len(uniq) > 1:
            print(f"[ВНИМАНИЕ] в файле несколько разных {key}: {uniq} "
                  f"— похоже, склеены две страницы, очистите nuxt.txt")

    width = max(len(k) for k in PATTERNS)
    for key, pattern in PATTERNS.items():
        m = re.search(pattern, text)
        value = (m.group(1) if m and m.lastindex else None)
        print(f"{key.ljust(width)} : {value if value else '— не найдено'}")

    m = re.search(PATTERNS["товаров_в_категории"], text)
    if m:
        print(f"\nИТОГО товаров в категории: {int(m.group(1)):,}".replace(",", " "))

if __name__ == "__main__":
    main()