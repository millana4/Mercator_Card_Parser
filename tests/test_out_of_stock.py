# -*- coding: utf-8 -*-
"""
test_out_of_stock.py — тест различителя "товар закончился" vs антибот.
Проверяет логику: редирект на поиск С product_id нашего sku = out_of_stock,
БЕЗ него = антибот. Selenium не нужен — тестируем чистую логику различения.
Запуск: pytest tests/test_out_of_stock.py -v
"""
import re
import pytest


# Копия метода _sku_from_url из parser.py (чистая функция, без класса).
# Если поменяешь regex в parser.py — поменяй и здесь.
def sku_from_url(url):
    m = re.search(r"/product/(?:[^/]*-)?(\d+)", url)
    return m.group(1) if m else None


def is_out_of_stock_redirect(product_url: str, current_url: str) -> bool:
    """True = товар закончился (редирект с нашим product_id).
    False = редирект без нашего id (вероятно антибот)."""
    if "/search/" in current_url and "/product/" not in current_url:
        sku = sku_from_url(product_url)
        return bool(sku and f"product_id={sku}" in current_url)
    return False


class TestSkuFromUrl:
    def test_plain_url(self):
        assert sku_from_url("https://www.ozon.ru/product/3073451822/") == "3073451822"

    def test_url_with_slug(self):
        assert sku_from_url(
            "https://www.ozon.ru/product/dzhinsy-ferron-2935340326/"
        ) == "2935340326"

    def test_no_sku(self):
        assert sku_from_url("https://www.ozon.ru/search/") is None


class TestRedirectDiscriminator:
    def test_out_of_stock_redirect_with_our_product_id(self):
        # Реальный пример: товар закончился, Ozon увёл на поиск, сохранил product_id
        product_url = "https://www.ozon.ru/product/3973310017/"
        current_url = ("https://www.ozon.ru/search/?deny_category_prediction=true"
                       "&from_global=true&text=%D0%96%D0%B8%D0%BB%D0%B5%D1%82"
                       "&product_id=3973310017")
        assert is_out_of_stock_redirect(product_url, current_url) is True

    def test_antibot_redirect_without_product_id(self):
        # Настоящий антибот: редирект на поиск БЕЗ нашего product_id
        product_url = "https://www.ozon.ru/product/3073451822/"
        current_url = "https://www.ozon.ru/search/?text=something"
        assert is_out_of_stock_redirect(product_url, current_url) is False

    def test_antibot_redirect_with_foreign_product_id(self):
        # Редирект с ЧУЖИМ product_id — не наш товар, не считаем out_of_stock
        product_url = "https://www.ozon.ru/product/3073451822/"
        current_url = "https://www.ozon.ru/search/?text=x&product_id=9999999999"
        assert is_out_of_stock_redirect(product_url, current_url) is False

    def test_normal_page_no_redirect(self):
        # Карточка открылась нормально — не редирект вообще
        product_url = "https://www.ozon.ru/product/3073451822/"
        current_url = "https://www.ozon.ru/product/3073451822/"
        assert is_out_of_stock_redirect(product_url, current_url) is False