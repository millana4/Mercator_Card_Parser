import re
import json
import html as htmllib

from pydantic import BaseModel, Field

from app.core.logging import get_logger

logger = get_logger(__name__)


class Candidate(BaseModel):
    """Кандидат из поисковой выдачи (облегчённые данные, без захода в карточку)."""
    sku: str = Field(..., description="Артикул товара")
    url: str | None = Field(None, description="URL карточки (без query-хвоста)")
    slug: str | None = Field(None, description="Slug из URL без артикула (для дедупа)")
    name: str | None = Field(None, description="Название товара с плитки")
    price: float | None = Field(None, description="Цена, руб. (число)")
    original_price: float | None = Field(None, description="Цена без скидки, руб.")
    seller: str | None = Field(None, description="Магазин/продавец (из карточки)")
    rating: float | None = Field(None, description="Рейтинг (например, 4.8)")
    reviews: int | None = Field(None, description="Число отзывов")
    is_original: bool = Field(False, description="Есть ли пометка 'Оригинал'")

def _tilegrid_state(html: str) -> dict | None:
    """Найти data-state виджета плиток выдачи (tileGridDesktop)."""
    m = re.search(r'id="(state-tileGridDesktop-\d+-default-\d+)"', html)
    if not m:
        return None
    state_id = m.group(1)
    patterns = [
        r'id="' + re.escape(state_id) + r'"[^>]*?\sdata-state="(.*?)"(?:\s|>)',
        r'\sdata-state="(.*?)"[^>]*?id="' + re.escape(state_id) + r'"',
    ]
    for p in patterns:
        mm = re.search(p, html, re.DOTALL)
        if mm:
            try:
                return json.loads(htmllib.unescape(mm.group(1)))
            except json.JSONDecodeError:
                return None
    return None

def _clean_num(text: str | None) -> float | None:
    """'528 ₽' -> 528.0, '4.8' -> 4.8. Только цифры и один десятичный разделитель."""
    if not text:
        return None
    m = re.search(r"[.,](\d{1,2})\D*$", text)
    dec = ""
    s = text
    if m:
        dec = m.group(1);
        s = s[:m.start()]
    integer = re.sub(r"\D", "", s)
    if not integer and not dec:
        return None
    try:
        return float((integer or "0") + ("." + dec if dec else ""))
    except ValueError:
        return None

def _slug_from_link(link: str) -> tuple[str | None, str | None]:
    """
    Из '/product/komplekt-...-4345164734/?at=...' получить:
      url  -> https://www.ozon.ru/product/komplekt-...-4345164734/
      slug -> komplekt-...  (без артикула и без хвоста — для дедупа)
    """
    if not link:
        return None, None
    path = link.split("?")[0]
    url = "https://www.ozon.ru" + path if path.startswith("/") else path
    m = re.search(r"/product/(.+?)-(\d+)/?$", path)
    slug = m.group(1) if m else None
    return url, slug

def _candidates_from_state(st: dict, seen: set[str]) -> list[Candidate]:
    """Плитки одного виджета выдачи -> кандидаты. seen - уже встреченные sku
    (дубли листинга и плитки из соседних виджетов пропускаются)."""
    out: list[Candidate] = []
    for it in st.get("items", []):
        sku = str(it.get("sku") or it.get("id") or "").strip()
        if not sku or sku in seen:
            continue
        seen.add(sku)

        url, slug = _slug_from_link(it.get("action", {}).get("link", ""))
        main = it.get("mainState", [])

        price = original = None
        name = None
        rating = reviews = None
        is_original = False

        for b in main:
            btype = b.get("type")
            if btype == "priceV2":
                for p in b["priceV2"].get("price", []):
                    if p.get("textStyle") == "PRICE":
                        price = _clean_num(p.get("text"))
                    elif p.get("textStyle") == "ORIGINAL_PRICE":
                        original = _clean_num(p.get("text"))
            elif btype == "textDS":
                ds = b["textDS"]
                if b.get("id") == "name" or ds.get("testInfo", {}).get("automatizationId") == "tile-name":
                    name = ds.get("text")
            elif btype == "labelListV2":
                ll = b["labelListV2"]
                auto = ll.get("testInfo", {}).get("automatizationId")
                texts = [x["text"]["text"] for x in ll.get("items", []) if x.get("type") == "text"]
                if auto == "tile-list-rating":
                    for t in texts:
                        if re.fullmatch(r"[\d.,]+", t):
                            rating = _clean_num(t)
                        elif "отзыв" in t:
                            reviews = int(re.sub(r"\D", "", t) or 0)
                # пометка «Оригинал» может быть в отдельном labelListV2
                if any("Оригинал" in t for t in texts):
                    is_original = True

        out.append(Candidate(
            sku=sku, url=url, slug=slug, name=name,
            price=price, original_price=original,
            rating=rating, reviews=reviews, is_original=is_original,
        ))
    return out


def extract_candidates(html: str) -> list[Candidate]:
    """
    Разобрать HTML страницы выдачи Ozon в список кандидатов.
    Источник — первый виджет tileGridDesktop (плитки, пришедшие со страницей).
    Дедуплицирует по артикулу в рамках одного вызова (дубли листинга).
    """
    st = _tilegrid_state(html)
    if not st or "items" not in st:
        logger.warning("extract_candidates: виджет плиток не найден или пуст")
        return []
    out = _candidates_from_state(st, set())
    logger.info("extract_candidates: получено %s кандидатов", len(out))
    return out

def passes_quality(c: Candidate, min_rating: float, min_reviews: int) -> bool:
    """Порог качества по данным выдачи: рейтинг и число отзывов.
    reviews строго больше min_reviews (по ТЗ: > 100)."""
    if c.rating is None or c.rating < min_rating:
        return False
    if c.reviews is None or c.reviews <= min_reviews:
        return False
    return True

def _norm_name(name: str | None) -> str:
    """Нормализовать название для сравнения: нижний регистр, схлопнуть пробелы."""
    if not name:
        return ""
    return re.sub(r"\s+", " ", name.strip().lower())

def dedupe(cands: list[Candidate]) -> list[Candidate]:
    """Убрать варианты одного товара внутри выдачи: дубли по slug и по названию.
    Первый по порядку выдачи остаётся, последующие с тем же slug/именем — выбывают."""
    seen_slug: set[str] = set()
    seen_name: set[str] = set()
    out: list[Candidate] = []
    for c in cands:
        slug = c.slug or ""
        name = _norm_name(c.name)
        if slug and slug in seen_slug:
            continue
        if name and name in seen_name:
            continue
        if slug:
            seen_slug.add(slug)
        if name:
            seen_name.add(name)
        out.append(c)
    return out

def drop_excluded(cands: list[Candidate], exclude: list) -> list[Candidate]:
    """Убрать кандидатов, совпадающих с уже подобранными (исключениями):
    по артикулу, по slug или по названию — как обсуждали (дубли к существующим)."""
    ex_sku = {str(e.sku) for e in exclude if getattr(e, "sku", None)}
    ex_slug = {_slug_only(e.url) for e in exclude if getattr(e, "url", None)}
    ex_slug = {s for s in ex_slug if s}
    ex_name = {_norm_name(getattr(e, "name", None)) for e in exclude}
    ex_name = {n for n in ex_name if n}

    out = []
    for c in cands:
        if c.sku in ex_sku:
            continue
        if c.slug and c.slug in ex_slug:
            continue
        if _norm_name(c.name) in ex_name:
            continue
        out.append(c)
    return out

def _slug_only(url: str | None) -> str | None:
    """Из URL исключения вытащить slug без артикула (как _slug_from_link, но из готового url)."""
    if not url:
        return None
    m = re.search(r"/product/(.+?)-(\d+)/?", url.split("?")[0])
    return m.group(1) if m else None

def prioritize_original(cands: list[Candidate]) -> list[Candidate]:
    """Карточки с пометкой 'Оригинал' в первую очередь проверять"""
    originals = [c for c in cands if c.is_original]
    rest = [c for c in cands if not c.is_original]
    return originals + rest

def prefilter(cands: list[Candidate], exclude: list,
              min_rating: float, min_reviews: int) -> list[Candidate]:
    """Полный первичный отсев по выдаче в порядке:
      1) дедуп вариантов одного товара внутри выдачи;
      2) отсев по рейтингу и отзывам;
      3) отсев дублей к исключениям;
      4) приоритизация 'Оригинал'.
    Возвращает кандидатов, готовых к заходу в карточку."""
    step = dedupe(cands)
    step = [c for c in step if passes_quality(c, min_rating, min_reviews)]
    step = drop_excluded(step, exclude)
    step = prioritize_original(step)
    return step


def extract_next_page(html: str) -> str | None:
    """
    Достать URL следующей страницы выдачи из виджета infiniteVirtualPaginator.
    Возвращает абсолютный URL или None, если следующей страницы нет (конец выдачи).
    """
    m = re.search(r'id="(state-infiniteVirtualPaginator-\d+-default-\d+)"', html)
    if not m:
        return None
    state_id = m.group(1)
    for p in [r'id="' + re.escape(state_id) + r'"[^>]*?\sdata-state="(.*?)"(?:\s|>)',
              r'\sdata-state="(.*?)"[^>]*?id="' + re.escape(state_id) + r'"']:
        mm = re.search(p, html, re.DOTALL)
        if mm:
            try:
                st = json.loads(htmllib.unescape(mm.group(1)))
            except json.JSONDecodeError:
                return None
            nxt = st.get("nextPage")
            if not nxt:
                return None
            # nextPage относительный и может нести HTML-сущности → нормализуем
            nxt = htmllib.unescape(nxt)
            if nxt.startswith("/"):
                nxt = "https://www.ozon.ru" + nxt
            return nxt
    return None


def extract_pagination(nuxt_state: dict) -> tuple[int | None, int | None]:
    """
    Достать (currentPage, totalPages) из __NUXT__ выдачи.
    Лежат в shared.catalog. totalPages — скользящая оценка: растёт по мере
    листания, пока есть товары. Признак 'есть следующая' = currentPage < totalPages.
    """
    try:
        catalog = nuxt_state.get("shared", {}).get("catalog", {})
        current = catalog.get("currentPage")
        total = catalog.get("totalPages")
        return current, total
    except (AttributeError, TypeError):
        return None, None


def build_next_page_url(current_url: str, current_page: int) -> str:
    """Построить URL следующей страницы, заменив/добавив page=current_page+1."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(current_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(current_page + 1)]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


# ---------- Плитки выдачи из отрисованной страницы ----------
# Ozon кладёт в data-state только первые 8 плиток, остальные дорисовывает скриптом.
# Плитка разбирается по тексту: классы в разметке зашифрованы и меняются.

class SearchTile(BaseModel):
    """Плитка поисковой выдачи (в карточку не заходим)."""
    position: int = Field(..., description="Позиция в выдаче, начиная с 1")
    sku: str = Field(..., description="Артикул")
    url: str | None = Field(None, description="URL карточки (без query-хвоста)")
    name: str | None = Field(None, description="Название с плитки")
    brand: str | None = Field(None, description="Бренд, если показан на плитке")
    brand_verified: bool = Field(False, description="Отметка «Бренд проверен»")
    price: float | None = Field(None, description="Цена, руб.")
    original_price: float | None = Field(None, description="Зачёркнутая цена, руб.")
    discount_pct: int | None = Field(None, description="Скидка с плитки, %")
    quantity: int | None = Field(None, description="Остаток с плитки («51 ед осталось»)")
    rating: float | None = Field(None, description="Рейтинг")
    reviews: int | None = Field(None, description="Число отзывов")
    badges: list[str] = Field(default_factory=list, description="Промо-бейджи («Скидки недели»)")
    labels: list[str] = Field(default_factory=list,
                              description="Прочие метки плитки: известные («Стало дешевле») и после рейтинга («Ozon»)")


_NUM_SPACES = "\u00a0\u2009\u202f "
_PRICE_RE = re.compile(r"^[\d" + _NUM_SPACES + r"]+₽$")
_DISCOUNT_RE = re.compile(r"^[−\-–]\s?(\d{1,3})\s?%$")
_STOCK_LEFT_RE = re.compile(r"(\d[\d" + _NUM_SPACES + r"]*)\s*(?:ед\.?|шт\.?)\s*остал", re.IGNORECASE)
_LEFT_STOCK_RE = re.compile(r"остал\w*\s+(\d[\d" + _NUM_SPACES + r"]*)\s*(?:ед\.?|шт\.?)", re.IGNORECASE)
_RATING_RE = re.compile(r"^\d[.,]\d$")
_REVIEWS_RE = re.compile(r"^(\d[\d" + _NUM_SPACES + r"]*)\s*отзыв", re.IGNORECASE)
_BARE_NUM_RE = re.compile(r"^\d[\d" + _NUM_SPACES + r"]*$")
_SKU_FROM_HREF_RE = re.compile(r"/product/(?:[^/]*-)?(\d+)/?$")
BRAND_VERIFIED = "Бренд проверен"
# Метки Ozon, которые стоят на месте бренда (между ценой и названием) - узнаём по тексту.
# Новая метка, встреченная в данных, добавляется сюда.
KNOWN_LABELS = frozenset({"Стало дешевле"})


def _digits(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def _stock_left(text: str) -> int | None:
    """«51 ед осталось» / «Осталась 1 шт» -> число; иначе None."""
    m = _STOCK_LEFT_RE.search(text) or _LEFT_STOCK_RE.search(text)
    return _digits(m.group(1)) if m else None


def parse_tile(href: str | None, texts: list[str], position: int) -> SearchTile | None:
    """Плитка: ссылка + текстовые фрагменты по порядку -> SearchTile.
    До первой цены - бейджи; после цены до рейтинга - бренд и название
    (название - последний фрагмент); после рейтинга - отзывы и метки.
    None - в ссылке нет артикула (не плитка товара)."""
    path = (href or "").split("?")[0]
    m = _SKU_FROM_HREF_RE.search(path)
    if not m:
        return None
    url, _ = _slug_from_link(href or "")

    prices: list[float] = []
    discount = quantity = reviews = None
    rating = None
    brand = None
    brand_verified = False
    badges: list[str] = []
    body: list[str] = []      # после цены до рейтинга: бренд, название
    labels: list[str] = []    # после рейтинга
    after_rating = False

    for raw in texts:
        t = (raw or "").strip()
        if not t:
            continue
        if _PRICE_RE.match(t):
            prices.append(_clean_num(t))
            continue
        md = _DISCOUNT_RE.match(t)
        if md:
            discount = int(md.group(1))
            continue
        q = _stock_left(t)
        if q is not None:
            quantity = q
            continue
        if t in KNOWN_LABELS:
            labels.append(t)
            continue
        if t == BRAND_VERIFIED:
            brand_verified = True
            # бренд, если показан, стоит прямо перед отметкой; название идёт после неё
            if body and brand is None:
                brand = body.pop()
            continue
        if prices and rating is None and _RATING_RE.match(t):
            rating = float(t.replace(",", "."))
            after_rating = True
            continue
        mr = _REVIEWS_RE.match(t)
        if mr:
            reviews = _digits(mr.group(1))
            continue
        if after_rating and reviews is None and _BARE_NUM_RE.match(t):
            reviews = _digits(t)          # «2» без слова «отзыва»
            continue
        if not prices:
            badges.append(t)
        elif after_rating:
            labels.append(t)
        else:
            body.append(t)

    if brand is None and len(body) >= 2:
        brand = body[-2]                  # бренд без отметки - прямо перед названием

    return SearchTile(
        position=position, sku=m.group(1), url=url,
        name=body[-1] if body else None,
        brand=brand, brand_verified=brand_verified,
        price=prices[0] if prices else None,
        original_price=prices[1] if len(prices) > 1 else None,
        discount_pct=discount, quantity=quantity,
        rating=rating, reviews=reviews, badges=badges, labels=labels,
    )


def parse_tiles(raw_tiles: list[dict], limit: int) -> list[SearchTile]:
    """Плитки страницы [{href, texts}] в порядке на странице -> не больше limit
    плиток. Позиция = номер плитки на странице; повторный sku пропускается."""
    out: list[SearchTile] = []
    seen: set[str] = set()
    for i, rt in enumerate(raw_tiles, start=1):
        tile = parse_tile(rt.get("href"), rt.get("texts") or [], i)
        if tile is None or tile.sku in seen:
            continue
        seen.add(tile.sku)
        out.append(tile)
        if len(out) >= limit:
            break
    return out




def candidates_from_tiles(tiles: list[SearchTile]) -> list[Candidate]:
    """Плитки с отрисованной страницы (подбор с прокруткой) -> кандидаты для отсева.
    В data-state только первые 8 плиток, поэтому подбор читает плитки так же, как
    снимок выдачи. Пометка «Оригинал» - по меткам и бейджам плитки."""
    return [
        Candidate(
            sku=t.sku, url=t.url, slug=_slug_only(t.url), name=t.name,
            price=t.price, original_price=t.original_price,
            rating=t.rating, reviews=t.reviews,
            is_original=any("Оригинал" in x for x in t.labels + t.badges),
        )
        for t in tiles
    ]