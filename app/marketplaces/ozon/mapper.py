"""
mapper.py — преобразование сырых данных страницы Ozon в OzonCard.

Откуда берутся данные.
  Ozon встраивает состояние каждого виджета прямо в HTML, в атрибут
  data-state у элемента с id="state-<виджет>-<id>-default-1". Значение —
  это JSON с экранированными кавычками (&quot;). Поэтому основной источник
  структурированных данных — именно HTML, а не __NUXT__.state (там в основном
  layout-структура и SEO).

  Дополнительно используем __NUXT__ (если передан) для категории/иерархии и
  как запасной источник sku/url.

Главная функция: build_card(html, nuxt_state, url) -> OzonCard
"""

import re
import json
import html as htmllib
from app.core.logging import get_logger
from datetime import datetime, timezone

from app.marketplaces.ozon.models import OzonCard, OzonPrice, OzonSeller, OzonVariant, OzonLocation

logger = get_logger(__name__)


# ----------------------------------------------------------------------------- #
#  Вспомогательное: достать data-state конкретного виджета из HTML               #
# ----------------------------------------------------------------------------- #
def _widget_state(html: str, widget_name: str) -> dict | None:
    """
    Находит первый элемент id="state-<widget_name>-..." и парсит его data-state.
    Возвращает dict или None, если виджета на странице нет.
    """
    # id виджета вида: state-webPrice-3121879-default-1
    id_pat = re.compile(r'id="(state-' + re.escape(widget_name) + r'-\d+-default-\d+)"')
    m = id_pat.search(html)
    if not m:
        return None
    state_id = m.group(1)

    # data-state может идти после id или перед ним — пробуем оба порядка.
    patterns = [
        r'id="' + re.escape(state_id) + r'"[^>]*?\sdata-state="(.*?)"(?:\s|>)',
        r'\sdata-state="(.*?)"[^>]*?id="' + re.escape(state_id) + r'"',
    ]
    for p in patterns:
        mm = re.search(p, html, re.DOTALL)
        if mm:
            raw = htmllib.unescape(mm.group(1))
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️ Не удалось разобрать data-state виджета {widget_name}: {e}")
                return None
    return None


def _rs_text(rs) -> str:
    """
    Склеивает «rich-string» Ozon — массив фрагментов [{'type':'text','content':...}]
    — в обычную строку. Игнорирует newLine и нетекстовые куски.
    """
    if isinstance(rs, str):
        return rs
    if not isinstance(rs, list):
        return ""
    parts = []
    for item in rs:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("content", ""))
    return "".join(parts)


# ----------------------------------------------------------------------------- #
#  Извлечение отдельных полей                                                    #
# ----------------------------------------------------------------------------- #
def _extract_variants(html: str) -> tuple[str | None, list[OzonVariant]]:
    """
    Варианты товара (обычно размеры) из виджета webAspects.
    Каждый вариант — отдельный sku со своей доступностью (inStock/outOfStock),
    значением (напр. '48 RU / L') и ценой. Возвращает (имя_аспекта, список).
    Берём первый аспект (как правило, 'Размер').
    """
    st = _widget_state(html, "webAspects")
    if not st:
        return None, []
    aspects = st.get("aspects", [])
    if not aspects:
        return None, []

    asp = aspects[0]
    aspect_name = asp.get("aspectName")
    variants: list[OzonVariant] = []
    for v in asp.get("variants", []):
        data = v.get("data", {}) if isinstance(v.get("data"), dict) else {}
        value = data.get("searchableText") or _rs_text(data.get("textRs", []))
        variants.append(OzonVariant(
            sku=str(v.get("sku")) if v.get("sku") else None,
            value=value or None,
            availability=v.get("availability"),
            price=_clean_price(data.get("price")),
        ))
    return aspect_name, variants


def _extract_quantity(html: str) -> int | None:
    """
    Остаток товара. На странице распродажи лежит в виджете bigPromoPDP:
      stockNumber.text = '50'. Возвращаем число 50 или None, если блока нет.
    """
    st = _widget_state(html, "bigPromoPDP")
    if not st:
        return None

    number = None
    text = None

    def walk(node):
        nonlocal number, text
        if isinstance(node, dict):
            if node.get("stockNumber") and isinstance(node["stockNumber"], dict):
                number = node["stockNumber"].get("text") or number
            if node.get("stockText") and isinstance(node["stockText"], dict):
                text = node["stockText"].get("text") or text
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(st)
    if number:
        import re as _re
        digits = _re.sub(r"\D", "", str(number))
        return int(digits) if digits else None
    return None


def _extract_name(html: str, nuxt: dict | None) -> str | None:
    st = _widget_state(html, "webProductHeading")
    if st and st.get("title"):
        return st["title"]
    # запасной вариант — из SEO og:title или layoutTrackingInfo нет имени, берём seo
    if nuxt:
        seo = nuxt.get("seo", {})
        for meta in seo.get("meta", []):
            if meta.get("property") == "og:title":
                return meta.get("content", "").strip() or None
    return None


def _extract_brand(html: str, nuxt: dict | None) -> str | None:
    # Бренд надёжнее всего лежит в SEO JSON-LD (script[type=ld+json]).
    ld = _ld_json(nuxt)
    if ld and ld.get("brand"):
        return ld["brand"]
    return None


def _clean_price(value: str | None) -> float | None:
    """
    Привести цену к числу в рублях: '1 999 ₽' -> 1999.0.
    Убирает валюту и пробелы-разделители разрядов (в т.ч. неразрывные).
    Если присутствует дробная часть (копейки) через ',' или '.', она сохраняется.
    Возвращает None, если цифр в строке нет.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value)
    # десятичный разделитель: последняя ',' или '.', за которой идут ровно 1-2 цифры
    m = re.search(r"[.,](\d{1,2})\D*$", s)
    decimals = ""
    if m:
        decimals = m.group(1)
        s = s[:m.start()]

    integer = re.sub(r"\D", "", s)
    if not integer and not decimals:
        return None

    number = (integer or "0") + ("." + decimals if decimals else "")
    try:
        return float(number)
    except ValueError:
        return None


def _extract_price(html: str) -> OzonPrice:
    st = _widget_state(html, "webPrice")
    if not st:
        return OzonPrice()
    return OzonPrice(
        card_price=_clean_price(st.get("cardPrice")),
        price=_clean_price(st.get("price")),
        original_price=_clean_price(st.get("originalPrice")),
    )


def _extract_rating_reviews(html: str, nuxt: dict | None) -> tuple[str | None, str | None]:
    # webSingleProductScore: text = "4.9 • 103 отзыва"
    st = _widget_state(html, "webSingleProductScore")
    if st and st.get("text"):
        txt = st["text"]
        rating = None
        reviews = None
        mr = re.search(r"([\d.,]+)", txt)
        if mr:
            rating = mr.group(1)
        mc = re.search(r"•\s*(\d+)", txt)
        if mc:
            reviews = mc.group(1)
        if rating or reviews:
            return rating, reviews
    # запасной вариант — из JSON-LD aggregateRating
    ld = _ld_json(nuxt)
    if ld and isinstance(ld.get("aggregateRating"), dict):
        ar = ld["aggregateRating"]
        return (str(ar.get("ratingValue")) if ar.get("ratingValue") else None,
                str(ar.get("reviewCount")) if ar.get("reviewCount") else None)
    return None, None


def _extract_characteristics(html: str) -> dict[str, str]:
    """
    webShortCharacteristics.characteristics — список вида:
      {title.textRs -> 'Сезон', values[].text -> 'Демисезон'}
    Собираем плоский словарь {название: значение через запятую}.
    """
    st = _widget_state(html, "webShortCharacteristics")
    out: dict[str, str] = {}
    if not st:
        return out
    for ch in st.get("characteristics", []):
        title = _rs_text(ch.get("title", {}).get("textRs", []))
        values = ch.get("values", [])
        # У значений бывает «висящая» запятая внутри text ("Вискоза, "),
        # поэтому сначала чистим каждый кусок, потом склеиваем через запятую.
        cleaned = []
        for v in values:
            t = (v.get("text") or "").strip().strip(",").strip()
            if t:
                cleaned.append(t)
        joined = ", ".join(cleaned)
        if title and joined:
            out[title] = joined
    return out


def _extract_seller(html: str) -> OzonSeller:
    st = _widget_state(html, "webCurrentSeller")
    seller = OzonSeller()
    if not st:
        return seller

    # Название магазина и sellerId — внутри header/badge/action/params.sellerId.
    # Пройдёмся по сырой строке: sellerId легко достать регэкспом.
    raw = json.dumps(st, ensure_ascii=False)
    mid = re.search(r'"sellerId"\s*:\s*"?(\d+)"?', raw)
    if mid:
        seller.id = mid.group(1)

    # Юрданные — в tooltip.subtitle (массив text-фрагментов):
    #   "АО ЗАРИНА МФГ", newLine, newLine, "ОГРН - 1257800110105", ...
    # Найдём subtitle и распарсим.
    legal_name, ogrn = _seller_legal_from_tooltip(st)
    seller.legal_name = legal_name
    seller.ogrn = ogrn

    # Название магазина (часто = тексту заголовка продавца). Попробуем найти
    # человекочитаемое имя: ищем "title":{"text":"<имя>"} НЕ равное "Магазин"/"О магазине".
    for m in re.finditer(r'"text"\s*:\s*"([^"]+)"', raw):
        cand = m.group(1)
        if cand and cand not in ("Магазин", "О магазине", "Подписаться",
                                 "Вы подписаны", "Перейти в магазин"):
            # эвристика: имя магазина обычно короткое и без пробелов-предложений
            if len(cand) <= 40 and "ОГРН" not in cand:
                seller.name = cand
                break
    return seller


def _seller_legal_from_tooltip(seller_state: dict) -> tuple[str | None, str | None]:
    """Ищем в любом вложенном subtitle пары 'АО/ООО/ИП ...' и 'ОГРН - ...'."""
    legal_name = None
    ogrn = None

    def walk(node):
        nonlocal legal_name, ogrn
        if isinstance(node, dict):
            # subtitle как массив text-фрагментов
            if "subtitle" in node and isinstance(node["subtitle"], list):
                texts = [x.get("content", "") for x in node["subtitle"]
                         if isinstance(x, dict) and x.get("type") == "text"]
                for t in texts:
                    if re.match(r"^\s*(ООО|АО|ПАО|ЗАО|ИП|ОАО)\b", t) and not legal_name:
                        legal_name = t.strip()
                    mo = re.search(r"ОГРН(?:ИП)?\s*[-:]?\s*(\d{13,15})", t)
                    if mo and not ogrn:
                        ogrn = mo.group(1)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(seller_state)
    return legal_name, ogrn


# ----------------------------------------------------------------------------- #
#  SEO / JSON-LD и категория из __NUXT__                                         #
# ----------------------------------------------------------------------------- #
def _ld_json(nuxt: dict | None) -> dict | None:
    """Достаёт товарный JSON-LD (schema.org/Product) из seo.script."""
    if not nuxt:
        return None
    seo = nuxt.get("seo", {})
    for sc in seo.get("script", []):
        inner = sc.get("innerHTML")
        if inner and "schema.org" in inner and "Product" in inner:
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return None
    return None


def _extract_description(nuxt: dict | None) -> str | None:
    ld = _ld_json(nuxt)
    if ld and ld.get("description"):
        return ld["description"]
    return None


def _extract_category(nuxt: dict | None) -> tuple[str | None, str | None]:
    if not nuxt:
        return None, None
    lti = nuxt.get("layoutTrackingInfo", {})
    return lti.get("categoryName"), lti.get("hierarchy")


def _extract_sku_url(nuxt: dict | None, url: str | None) -> tuple[str | None, str | None]:
    sku = None
    canonical = None
    if nuxt:
        pi = nuxt.get("pageInfo", {})
        ai = pi.get("analyticsInfo", {})
        if ai.get("sku"):
            sku = str(ai["sku"])
        seo = nuxt.get("seo", {})
        for ln in seo.get("link", []):
            if ln.get("rel") == "canonical":
                canonical = ln.get("href")
                break
    if not sku and url:
        m = re.search(r"-(\d+)/?(?:\?|$)", url)
        if m:
            sku = m.group(1)
    return sku, (canonical or url)


def _strip_query(url: str | None) -> str | None:
    """Отрезать query-хвост после '?' (utm/at-метки): ...-123/?x=1 -> ...-123/."""
    if not url:
        return url
    return url.split("?", 1)[0]


def _extract_location(nuxt: dict | None) -> OzonLocation:
    """
    Регион выдачи из location.current сырого состояния.
    Берём город, страну, код страны, areaId, fias, таймзону.
    """
    if not nuxt:
        return OzonLocation()
    loc = nuxt.get("location")
    if not isinstance(loc, dict):
        return OzonLocation()
    cur = loc.get("current") if isinstance(loc.get("current"), dict) else loc

    area_id = cur.get("areaId")
    try:
        area_id = int(area_id) if area_id is not None else None
    except (TypeError, ValueError):
        area_id = None

    return OzonLocation(
        city=cur.get("name") or cur.get("city"),
        country=cur.get("country"),
        country_code=cur.get("countryCode"),
        area_id=area_id,
        fias=cur.get("fias"),
        timezone=cur.get("timeZoneUtcname"),
    )

def _extract_button_state(html: str, nuxt: dict | None) -> str:
    """
    Состояние товара по признакам страницы (надёжнее, чем гадать по ценам):
      - in_cart      — есть виджет webAddToCart (кнопка «В корзину»), товар в наличии;
      - out_of_stock — товара нет: webOutOfStock ИЛИ Ozon увёл на поиск
                       (pageType != 'pdp') ИЛИ webSale отдал «Товар закончился»;
      - not_found    — страницы не существует (ловится выше, в repository/parser);
      - unknown      — ничего из перечисленного не распозналось.
    """
    # 1. Явный виджет «нет в наличии».
    if 'state-webOutOfStock-' in html:
        return "out_of_stock"

    # 2. Ozon увёл со страницы товара на поиск аналогов: у живого pageType='pdp',
    #    у закончившегося становится 'search'.
    if nuxt:
        page_type = (nuxt.get("layoutTrackingInfo", {}).get("pageType")
                     or nuxt.get("pageInfo", {}).get("pageType"))
        if page_type and page_type != "pdp":
            return "out_of_stock"

    # 3. Есть кнопка «В корзину» — товар в наличии.
    if 'state-webAddToCart-' in html:
        return "in_cart"

    # 4. Подстраховка по тексту.
    if 'Товар закончился' in html or 'товар быстро закончился' in html:
        return "out_of_stock"

    return "unknown"


# ----------------------------------------------------------------------------- #
#  Главная сборка                                                               #
# ----------------------------------------------------------------------------- #
def build_card(html: str, nuxt_state: dict | None, url: str | None = None) -> OzonCard:
    """
    Собирает структурированную OzonCard из HTML страницы и (опц.) __NUXT__.state.

    html       — полный HTML карточки (там data-state виджетов);
    nuxt_state — словарь из __NUXT__.state (для SEO/категории/описания);
    url        — исходный URL (запасной источник sku).
    """
    sku, canonical = _extract_sku_url(nuxt_state, url)
    canonical = _strip_query(canonical)
    rating, reviews = _extract_rating_reviews(html, nuxt_state)
    category, category_path = _extract_category(nuxt_state)
    variants_aspect, variants = _extract_variants(html)
    if 'data-widget="error"' in html or "Такой страницы не существует" in html:
        button_state = "not_found"
    else:
        button_state = _extract_button_state(html, nuxt_state)

    card = OzonCard(
        sku=sku,
        url=canonical,
        parsed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        name=_extract_name(html, nuxt_state),
        brand=_extract_brand(html, nuxt_state),
        price=_extract_price(html),
        quantity=_extract_quantity(html),
        rating=rating,
        reviews_count=reviews,
        characteristics=_extract_characteristics(html),
        description=_extract_description(nuxt_state),
        category=category,
        category_path=category_path,
        variants_aspect=variants_aspect,
        variants=variants,
        seller=_extract_seller(html),
        location=_extract_location(nuxt_state),
        button_state=button_state,
    )
    return card