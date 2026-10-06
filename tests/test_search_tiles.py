# ---------- Разбор плиток поисковой выдачи (тексты реальных плиток Ozon) ----------
from app.marketplaces.ozon.search_listing import parse_tile, parse_tiles

H1 = "/product/longsliv-junion-iz-hlopkovogo-interloka-2-shtuki-v-nabore-yandeks-fabrika-3849530051/"
T1 = ["Скидки недели", "2 010 ₽", "5 011 ₽", "−59%", "51 ед осталось", "Бренд проверен",
      "Лонгслив Junion из хлопкового интерлока, 2 штуки в наборе, Яндекс Фабрика",
      "5.0", "951 отзыв"]


def test_full_tile_with_discount_and_stock():
    t = parse_tile(H1, T1, 1)
    assert t.sku == "3849530051" and t.position == 1
    assert t.url == "https://www.ozon.ru" + H1
    assert (t.price, t.original_price, t.discount_pct) == (2010, 5011, 59)
    assert t.quantity == 51
    assert t.badges == ["Скидки недели"]
    assert t.brand_verified and t.brand is None
    assert t.name.startswith("Лонгслив Junion")
    assert (t.rating, t.reviews) == (5.0, 951)


def test_tile_without_old_price():
    t = parse_tile("/product/longsliv-bossa-nova-shkolnaya-odezhda-1-sht-178130259/",
                   ["Цена что надо", "686 ₽", "Бренд проверен",
                    "Лонгслив Bossa Nova Школьная одежда 1 шт", "4.9", "2 526 отзывов"], 2)
    assert (t.price, t.original_price, t.discount_pct, t.quantity) == (686, None, None, None)
    assert t.badges == ["Цена что надо"]
    assert t.name == "Лонгслив Bossa Nova Школьная одежда 1 шт"
    assert (t.rating, t.reviews) == (4.9, 2526)


def test_tile_without_rating():
    t = parse_tile("/product/longsliv-moocie-1-sht-5354352319/",
                   ["Скидки недели", "622 ₽", "1 303 ₽", "−52%", "Лонгслив MOOCIE 1 шт"], 10)
    assert (t.price, t.original_price, t.discount_pct) == (622, 1303, 52)
    assert t.name == "Лонгслив MOOCIE 1 шт"
    assert (t.rating, t.reviews) == (None, None)


def test_stock_badge_before_price():
    t = parse_tile("/product/longsliv-bebeshechka-1-sht-3352308001/",
                   ["Осталась 1 шт", "1 805 ₽", "Лонгслив Бебешечка 1 шт", "5.0", "1 533 отзыва"], 21)
    assert t.quantity == 1
    assert t.badges == []
    assert (t.price, t.name, t.reviews) == (1805, "Лонгслив Бебешечка 1 шт", 1533)


def test_brand_before_verified_mark():
    t = parse_tile("/product/longsliv-vulpes-1-sht-1111111111/",
                   ["490 ₽", "950 ₽", "−48%", "25 ед осталось", "Vulpes", "Бренд проверен",
                    "Лонгслив 1 шт", "5.0", "48 отзывов"], 5)
    assert (t.brand, t.brand_verified, t.name, t.quantity) == ("Vulpes", True, "Лонгслив 1 шт", 25)


def test_parse_tiles_limit_duplicates_and_non_product():
    raw = [{"href": H1, "texts": T1},
           {"href": None, "texts": ["Реклама"]},
           {"href": H1, "texts": T1},
           {"href": "/product/longsliv-1-sht-4860180998/", "texts": ["757 ₽", "Лонгслив 1 шт"]},
           {"href": "/product/longsliv-1-sht-5050387423/", "texts": ["374 ₽", "Лонгслив 1 шт"]}]
    tiles = parse_tiles(raw, limit=2)
    assert [(t.sku, t.position) for t in tiles] == [("3849530051", 1), ("4860180998", 4)]
    

def test_brand_without_verified_mark():
    t = parse_tile("/product/bryuki-palatstso-milasha-shkolnye-bryuki-1-sht-2496905861/",
                   ["Скидки недели", "1\u2009557\u2009₽", "9\u2009999\u2009₽", "−84%",
                    "14\u2009ед осталось", "Милаша", "Брюки палаццо Школьные брюки, 1 шт",
                    "4.9", "1\u2009150\xa0отзывов"], 17)
    assert (t.brand, t.brand_verified) == ("Милаша", False)
    assert t.name == "Брюки палаццо Школьные брюки, 1 шт"
    assert (t.price, t.original_price, t.discount_pct, t.quantity) == (1557, 9999, 84, 14)
    assert (t.rating, t.reviews, t.labels) == (4.9, 1150, [])


def test_bare_reviews_number_and_label_after_rating():
    t = parse_tile("/product/bryuki-motherbear-1-sht-4995784154/",
                   ["Осталось 10 шт", "2\u2009915\u2009₽", "4\u2009190\u2009₽", "−30%",
                    "Брюки Motherbear, 1 шт", "5.0", "2", "Ozon"], 18)
    assert t.quantity == 10 and t.badges == []
    assert (t.name, t.brand) == ("Брюки Motherbear, 1 шт", None)
    assert (t.rating, t.reviews, t.labels) == (5.0, 2, ["Ozon"])


def test_known_label_is_not_brand():
    t = parse_tile("/product/bryuki-klassicheskie-keepitok-5369027945/",
                   ["2\u2009125\u2009₽", "10\u2009ед осталось", "Стало дешевле",
                    "Брюки классические KEEPITOK", "4.7", "47\xa0отзывов"], 17)
    assert (t.brand, t.name) == (None, "Брюки классические KEEPITOK")
    assert t.labels == ["Стало дешевле"]
    assert (t.price, t.quantity, t.rating, t.reviews) == (2125, 10, 4.7, 47)