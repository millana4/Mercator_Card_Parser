"""
mapper.py — преобразование сырых данных страницы Ozon в OzonCard.

Откуда берутся данные.
  Ozon встраивает состояние каждого виджета прямо в HTML, в атрибут
  data-state у элемента с id="state-<виджет>-<id>-default-1". Значение —
  это JSON с экранированными кавычками (&quot;). Поэтому основной источник
  структурированных данных — именно HTML, а не __NUXT__.state (там в основном
  layout-структура и SEO).

  Полный блок характеристик (webCharacteristics) и текст описания
  (webDescription) в data-state не лежат — они есть только в отрисованной
  разметке. Их разбираем по разметке виджета (data-widget="...").

  Дополнительно используем __NUXT__ (если передан) для категории/иерархии и
  как запасной источник sku/url.

Главная функция: build_card(html, nuxt_state, url) -> OzonCard
"""

import re
import json
import html as htmllib
from html.parser import HTMLParser
from app.core.logging import get_logger
from datetime import datetime, timezone

from app.marketplaces.ozon.models import (
    OzonCard, OzonPrice, OzonSeller, OzonVariant, OzonLocation, OzonAspect, OzonAspectValue,
)

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
#  Вспомогательное: разбор отрисованной разметки одного виджета                  #
# ----------------------------------------------------------------------------- #
# Теги без закрывающей пары — не меняют глубину вложенности.
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                        "link", "meta", "param", "source", "track", "wbr"})
# Служебные теги внутри виджета: заголовок «Описание», кнопка «Показать полностью»,
# иконки и скрипты. Их текст не берём.
_SKIP_TAGS = frozenset({"h2", "button", "script", "style", "svg"})


class _StopParsing(Exception):
    """Виджет закрылся — дальше документ не читаем."""


class _WidgetCollector(HTMLParser):
    """
    Читает разметку одного виджета data-widget="<имя>" и собирает:
      pairs  — пары <dt>ключ</dt><dd>значение</dd> в порядке страницы;
      blocks — текстовые фрагменты вне dt/dd и вне служебных тегов.
    Текст внутри dt/dd склеивается без разделителя: значение вида
    <a>В рубчик</a><span>, </span>Рюши даёт «В рубчик, Рюши».
    """

    def __init__(self, widget: str):
        super().__init__(convert_charrefs=True)
        self.widget = widget
        self.inside = False
        self.stack: list[str] = []
        self.pairs: list[tuple[str, str]] = []
        self.blocks: list[str] = []
        self._field: str | None = None
        self._buf: list[str] = []
        self._dt: str | None = None

    def handle_starttag(self, tag, attrs):
        if not self.inside:
            if dict(attrs).get("data-widget") == self.widget:
                self.inside = True
                self.stack = [tag]
            return
        if tag in _VOID_TAGS:
            return
        self.stack.append(tag)
        if tag in ("dt", "dd"):
            self._field = tag
            self._buf = []

    def handle_endtag(self, tag):
        if not self.inside or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            if top == self._field:
                self._close_field(top)
            if top == tag:
                break
        if not self.stack:
            raise _StopParsing

    def handle_data(self, data):
        if not self.inside or any(t in _SKIP_TAGS for t in self.stack):
            return
        if self._field:
            self._buf.append(data)
        elif data.strip():
            self.blocks.append(_norm_space(data))

    def _close_field(self, tag: str):
        text = _norm_space("".join(self._buf))
        if tag == "dt":
            self._dt = text
        elif self._dt:
            self.pairs.append((self._dt, text))
            self._dt = None
        self._field = None
        self._buf = []


def _norm_space(text: str) -> str:
    """Схлопнуть пробельные символы (в т.ч. неразрывные) в один пробел."""
    return re.sub(r"\s+", " ", text).strip()


def _clean_key(key: str) -> str:
    """Название характеристики без завершающего двоеточия и лишних пробелов:
    у части товаров Ozon пишет «Рисунок:» вместо «Рисунок»."""
    return _norm_space(key).rstrip(":").rstrip()


def _collect_widget(html: str, widget: str) -> _WidgetCollector | None:
    """
    Разобрать разметку первого виджета data-widget="<widget>".
    Чтение начинается с открывающего тега виджета и прекращается на его закрытии.
    Возвращает None, если виджета в разметке нет.
    """
    idx = html.find(f'data-widget="{widget}"')
    if idx < 0:
        return None
    start = html.rfind("<", 0, idx)
    collector = _WidgetCollector(widget)
    try:
        collector.feed(html[start:])
        collector.close()
    except _StopParsing:
        pass
    return collector


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


def _extract_aspects(html: str) -> list[OzonAspect]:
    """
    Все переключатели вариантов из webAspects: обычно «Цвет» (type=colorsMix)
    и «Размер» (type=sizes). У каждого значения свой sku; значение открытой
    карточки помечено active=true.
    """
    st = _widget_state(html, "webAspects")
    if not st:
        return []
    out: list[OzonAspect] = []
    for asp in st.get("aspects", []):
        values: list[OzonAspectValue] = []
        for v in asp.get("variants", []):
            data = v.get("data", {}) if isinstance(v.get("data"), dict) else {}
            value = data.get("searchableText") or _rs_text(data.get("textRs", []))
            raw_price = v.get("price") if v.get("price") is not None else data.get("price")
            values.append(OzonAspectValue(
                sku=str(v.get("sku")) if v.get("sku") else None,
                value=value or None,
                availability=v.get("availability"),
                price=_clean_price(raw_price),
                original_price=_clean_price(data.get("originalPrice")),
                active=bool(v.get("active")),
            ))
        out.append(OzonAspect(
            name=asp.get("aspectName"),
            key=asp.get("aspectKey"),
            type=asp.get("type"),
            values=values,
        ))
    return out


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
        title = _clean_key(_rs_text(ch.get("title", {}).get("textRs", [])))
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


def _extract_characteristics_full(html: str) -> dict[str, str]:
    """
    Полный блок характеристик из разметки виджета webCharacteristics:
      <dl><dt>Сезон</dt><dd>Демисезон</dd></dl> -> {'Сезон': 'Демисезон'}.
    Все пары без отбора, в порядке страницы. Если ключ встречается повторно
    с другим значением, значения склеиваются через запятую.
    Пустой словарь — блока в разметке нет.
    """
    collector = _collect_widget(html, "webCharacteristics")
    out: dict[str, str] = {}
    if not collector:
        return out
    for raw_key, value in collector.pairs:
        key = _clean_key(raw_key)
        if not key or not value:
            continue
        if key in out and out[key] != value:
            out[key] = f"{out[key]}, {value}"
        else:
            out[key] = value
    return out


def _extract_tnved(characteristics_full: dict[str, str]) -> str | None:
    """
    Код ТН ВЭД из полного блока характеристик: ключ, содержащий «ТН ВЭД»
    в любом написании (пробелы, дефис, точки, регистр). Возвращает только цифры
    значения или None, если такого ключа нет или в значении нет цифр.
    """
    for key, value in characteristics_full.items():
        if "ТНВЭД" in re.sub(r"[\s\-.]", "", key).upper():
            digits = re.sub(r"\D", "", value)
            return digits or None
    return None


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


def _extract_description(nuxt: dict | None, html: str | None = None) -> str | None:
    """
    Описание товара. Основной источник — JSON-LD: там чистый текст с переносами
    строк. В отрисованном виджете webDescription Ozon теряет часть слов
    (например, предлоги), поэтому разметка — только запасной источник:
    текстовые фрагменты виджета без заголовка и кнопки, через перенос строки.
    """
    ld = _ld_json(nuxt)
    if ld and ld.get("description"):
        return ld["description"]
    if html:
        collector = _collect_widget(html, "webDescription")
        if collector and collector.blocks:
            return "\n".join(collector.blocks)
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


# Главное фото: meta og:image (Ozon отдаёт вариант 600 px), запасной источник —
# image из товарного JSON-LD. Фото товара лежат в /multimedia…/; всё остальное
# (логотип Ozon на странице поиска, куда уводит закончившийся товар) отбрасываем.
_OG_IMAGE_TAG = re.compile(r'<meta\b[^>]*?\bproperty="og:image"[^>]*>', re.IGNORECASE)
_CONTENT_ATTR = re.compile(r'\bcontent="([^"]*)"', re.IGNORECASE)


def _product_image(url) -> str | None:
    """Адрес фото товара или None, если это не фото товара."""
    if isinstance(url, dict):
        url = url.get("url") or url.get("contentUrl")
    if not isinstance(url, str):
        return None
    url = htmllib.unescape(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http") or "/multimedia" not in url:
        return None
    return url


def _extract_image_url(html: str | None, nuxt: dict | None) -> str | None:
    """Адрес главного фото карточки: og:image, иначе image из JSON-LD, иначе None."""
    for tag in _OG_IMAGE_TAG.findall(html or ""):
        m = _CONTENT_ATTR.search(tag)
        url = _product_image(m.group(1)) if m else None
        if url:
            return url
    ld = _ld_json(nuxt)
    image = ld.get("image") if ld else None
    if isinstance(image, list):
        image = image[0] if image else None
    return _product_image(image)


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

    html       — полный HTML карточки (там data-state виджетов и разметка);
    nuxt_state — словарь из __NUXT__.state (для SEO/категории/описания);
    url        — исходный URL (запасной источник sku).
    """
    sku, canonical = _extract_sku_url(nuxt_state, url)
    canonical = _strip_query(canonical)
    rating, reviews = _extract_rating_reviews(html, nuxt_state)
    category, category_path = _extract_category(nuxt_state)
    variants_aspect, variants = _extract_variants(html)
    characteristics_full = _extract_characteristics_full(html)
    if 'data-widget="error"' in html or "Такой страницы не существует" in html:
        button_state = "not_found"
    else:
        button_state = _extract_button_state(html, nuxt_state)

    card = OzonCard(
        sku=sku,
        url=canonical,
        image_url=_extract_image_url(html, nuxt_state),
        parsed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        name=_extract_name(html, nuxt_state),
        brand=_extract_brand(html, nuxt_state),
        price=_extract_price(html),
        quantity=_extract_quantity(html),
        rating=rating,
        reviews_count=reviews,
        characteristics=_extract_characteristics(html),
        characteristics_full=characteristics_full,
        tnved=_extract_tnved(characteristics_full),
        description=_extract_description(nuxt_state, html),
        category=category,
        category_path=category_path,
        variants_aspect=variants_aspect,
        variants=variants,
        aspects=_extract_aspects(html),
        full_content=bool(characteristics_full),
        seller=_extract_seller(html),
        location=_extract_location(nuxt_state),
        button_state=button_state,
    )
    return card