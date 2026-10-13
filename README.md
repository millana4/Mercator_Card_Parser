# Mercator Card Parser

Сервис парсинга карточек маркетплейсов. Умеет подбирать карточки под страту
(например: «Рубашки женские, 4 шт.»), парсить данные карточки (на вход — URL или артикул,
на выход — сырой JSON `__NUXT__` или очищенные данные по модели) и снимать
поисковую выдачу по запросу (плитки с позициями).

Сейчас поддерживает только Ozon.

Работать с сервисом можно двумя способами: синхронно через HTTP (Swagger) или
асинхронно через Redis — для интеграции с таск-менеджером (ядром).

## Стек технологий

- **Python 3.13**
- **FastAPI** — HTTP-API, Swagger на `/docs`
- **Selenium** + CDP — управляемый браузер, согласованный профиль Windows (без selenium-stealth)
- **Google Chrome 152 (с окном) + Xvfb** — рендеринг страниц Ozon; свой виртуальный экран на сессию браузера
- **Pydantic / pydantic-settings** — модели данных и настройки
- **redis** — асинхронный транспорт задач (общий пул через списки)
- **Docker + Docker Compose** — упаковка и запуск (HTTP-сервис + воркер)
- **pytest** — тесты маппера, подбора, воркера, политики банов и отпечатка

## Структура приложения
```
app/
├── main.py                  # точка входа FastAPI
├── api/
│   ├── dependencies.py      # проверка ключа между сервисами
│   └── routes/              # HTTP-эндпоинты
├── categories/              # категорийные стратегии подбора
│   ├── base.py              # базовая стратегия
│   ├── clothing.py          # одежда: сезон, коллекция, уникальность продавца
│   ├── kids_clothing.py     # детская одежда
│   ├── models.py            # модели контракта подбора
│   └── registry.py          # реестр стратегий
├── core/
│   ├── config.py            # настройки из env: таймауты, паузы, бан, снимки блокировок
│   ├── exceptions.py        # доменные ошибки: антибот / сбой загрузки / нет в наличии / не найдено
│   ├── logging.py           # уровни логирования
│   └── security.py          # декодирование токена
├── chrome-profile/          # постоянный профиль Chrome воркера (не в git; сбрасывается после бана)
├── debug/                   # debug-артефакты и снимки блокировок block_*
├── marketplaces/
│   └── ozon/
│       ├── parser.py         # Selenium: браузер с профилем Windows, страница, выдача, детект блокировки
│       ├── device_profile.py # наборы устройств (экран, ядра, память, видеокарта), выбор по PARSER_ID
│       ├── display.py        # свой Xvfb на сессию браузера
│       ├── mapper.py         # сырьё -> доменная карточка OzonCard
│       ├── models.py         # доменные модели
│       ├── nuxt.py           # извлечение __NUXT__ из HTML
│       ├── repository.py     # новый браузер на вызов (только диагностика)
│       ├── schemas.py        # схемы API
│       ├── search_listing.py # выдача: кандидаты, плитки, пагинация
│       ├── selection.py      # подбор под страту
│       └── service.py        # оркестрация: одна сессия браузера, снимки блокировок, сброс профиля
├── shared/
│   └── logger.py            # лог событий
└── worker/                  # Redis-транспорт
    ├── run_worker.py        # точка входа воркера
    ├── consumer.py          # цикл задач, учёт блокировок, пауза после бана
    ├── ban_policy.py        # расчёт уровня бана и паузы
    ├── handlers.py          # задачи select / search / parse -> результат
    ├── result_producer.py   # отправка результатов и счётчиков
    └── redis_config.py      # ключи очередей, счётчиков, состояния бана
docker/fontconfig/           # правила шрифтов: Windows видны, Linux скрыты
tests/                       # тесты
utils/fp_check.py            # проверка отпечатка браузера без обращения к Ozon
.env                         # настройки окружения (не в git)
.env.example                 # шаблон настроек
docker-compose.yml           # сборка и запуск (worker + parsing-service)
Dockerfile                   # Chrome и chromedriver одной версии, шрифты, маркер cdc_ вырезан
entrypoint.sh                # запуск Xvfb (:99) + команды
requirements.txt
```

Swagger: http://localhost:8010/docs

## Эндпоинты Ozon
1. `POST /api/v1/ozon/card/by-url` — очищенная карточка по URL
2. `POST /api/v1/ozon/card/by-id` — очищенная карточка по артикулу
3. `POST /api/v1/ozon/select` — подбор карточек под страту
4. `POST /api/v1/ozon/category/by-url` — информация о категории по URL
5. `POST /api/v1/ozon/category/by-id` — информация о категории по ID
6. `POST /api/v1/ozon/raw/by-url` — сырой `__NUXT__` по URL
7. `POST /api/v1/ozon/raw/by-id` — сырой `__NUXT__` по артикулу
8. `POST /api/v1/ozon/search/tiles` — снимок поисковой выдачи (плитки с позициями, HTML и PNG сохраняются)
9. `POST /api/v1/ozon/search/diagnostics` — диагностика поисковой выдачи (служебный)

Файлы debug и служебные эндпоинты:
- `GET /api/v1/files`
- `GET /api/v1/files/{name}`
- `GET /api/v1/health`
- `GET /api/v1/health/ready`

Машиночитаемый контракт API: `http://localhost:8010/openapi.json`

## Работа через Redis (воркер)

Ключи: `<поток>:<tasks|results>:<маркетплейс>:<гео>`, например `parse:tasks:ozon:spb`.
Воркер берёт задачи BRPOP по приоритету: подбор (`select`) → снимок выдачи (`search`) → обход (`parse`)
и кладёт результат в `…:results:…` той же площадки. Все задачи идут в одной долгоживущей
сессии браузера и одном профиле.

Задача, которую воркер выполнял в момент остановки контейнера, пропадает:
ядро переотправит её по своему таймауту.

Счётчики (в Redis ядра): `parser:<id>:<мп>:done`, `antibot` (капчи), `load_failed`
(сбои загрузки), `search_done`, `search_antibot`. Состояние бана — `parser:<id>:<мп>:ban`.

## Защита от блокировок

**Темп на IP.** Пауза внутри карточки 15–30 с, между карточками обхода и между переходами
подбора — 50–90 с (около 25 карточек в час).

**Реакция на блокировку:**
- капча / блок / редирект на поиск без `product_id` — бан сразу;
- сбой загрузки (таймаут, нет `__NUXT__` без признаков капчи, ошибка браузера) — браузер
  пересоздаётся; три сбоя подряд — бан;
- бан: снимок (title, адрес, скриншот) в `app/debug/block_*` (хранятся последние 50), сброс
  профиля Chrome, следующий набор устройства, пауза 10 мин, каждая следующая вдвое длиннее,
  потолок 24 ч; после 24 ч без бана уровень обнуляется. Первая задача после паузы — пробная.
- состояние бана хранится в Redis — перезапуск контейнера паузу не обнуляет.

**Отпечаток — согласованный Windows 10 / Chrome 152:** окно на Xvfb, программный WebGL с
видеокартой из профиля устройства, User-Agent и Client Hints от версии установленного Chrome,
`navigator.webdriver = false`, шрифты Windows (Microsoft core fonts; Calibri, Cambria, Segoe UI —
метрические замены), Linux-шрифты скрыты, маркер chromedriver `cdc_` вырезан при сборке.

**Профиль устройства на парсер.** Восемь наборов (экран, ядра, память, видеокарта);
номер набора = `PARSER_ID` + поколение (поколение растёт при каждом бане).

**Известные ограничения:** в воркерах страницы (Web Worker) платформа, ядра и память —
реальные; рисунок canvas одинаковый у всех парсеров; отрисовка WebGL программная.

**Правило развёртывания:** не больше одного парсера у одного провайдера / в одной подсети —
Ozon блокирует адреса провайдера волнами, вплоть до блока на уровне сети.

## Проверка отпечатка

Открывает локальную страницу в браузере парсера (к Ozon не обращается) и сверяет заголовки и
свойства браузера с профилем Windows и устройства. Ожидается `провалов 0 из 30`.

```bash
docker compose run --rm worker python -m utils.fp_check | grep -E "FAIL|ИТОГ"
```

Необязательный аргумент — поколение устройства: `python -m utils.fp_check 1`.

## Развёртывание нового экземпляра парсера

Каждый парсер — отдельный сервер со своим внешним IP; все читают задачи из общего Redis
на сервере ядра. Для сборки образа нужен доступ к `dl.google.com`, `storage.googleapis.com`,
`github.com` и SourceForge.

### 1. Установить Docker

Если на сервере ещё нет Docker, установить его и добавить пользователя в группу `docker`:

```bash
sudo usermod -aG docker $USER
```

Выйти из SSH и зайти снова — группа применяется при новом входе. `docker ps` должен работать без `sudo`.

### 2. Склонировать репозиторий в папку `mercator_parser`

```bash
cd ~
git clone https://github.com/millana4/Mercator_Card_Parser mercator_parser
cd mercator_parser
```

Папку `app/chrome-profile` со старых машин не копировать: новый парсер начинает с пустого профиля.

### 3. Создать и заполнить `.env`

```bash
cp .env.example .env
nano .env
```

Индивидуально под сервер:

- `PARSER_ID` — уникальный номер, не повторяющийся с другими и с уже использованными
  (иначе сольются счётчики; от номера зависит и набор устройства);
- `REDIS_HOST` — внешний адрес сервера ядра с Redis;
- `REDIS_PASSWORD` — пароль Redis (из `.env` ядра).

Остальное — как на других парсерах. Паузы и параметры бана в `.env` не задавать:
работают значения по умолчанию из кода.

### 4. Собрать и проверить отпечаток

```bash
docker compose build
docker compose run --rm worker python -m utils.fp_check | grep -E "FAIL|ИТОГ"
```

### 5. Запустить и проверить

```bash
docker compose up -d
docker compose logs --tail 20 worker
```

В логе: `Воркер запущен | … parser_id=<номер>`, `TaskConsumer подключён к Redis`,
`Поколение устройства браузера: 0`, затем `Получена задача` и `ok=True`.

Логи в реальном времени: `docker compose logs -f worker`.

### 6. Диагностика

```bash
c=mercator_parser-worker-1
echo "За 24ч: успешно=$(docker logs --since 24h $c 2>&1 | grep -c 'ok=True') | провалов=$(docker logs --since 24h $c 2>&1 | grep -c 'ok=False')"
echo "Баны и сбои загрузки:"
docker logs --since 24h $c 2>&1 | grep -E "БАН|Сбой загрузки подряд|Пауза после бана" | tail -5
echo "Снимки блокировок:"; ls app/debug | grep block_ | tail -3
```

Состояние бана и счётчики — на сервере ядра:

```bash
docker exec tm-redis redis-cli -a "<пароль>" --no-auth-warning HGETALL parser:<id>:ozon:ban
```

## Обновление парсера

Код на машине парсера не редактируется: правки — в репозитории, на машине только `git pull`.

```bash
cd ~/mercator_parser
git pull --ff-only
docker compose up -d --build
docker compose logs --tail 20 worker
```

`.env` правится вручную (`nano .env`); после правки — `docker compose up -d --force-recreate`.

При переходе со старого кода (до защиты от блокировок, октябрь 2026):

```bash
git pull --ff-only
sed -i '/^OZON_HEADLESS/d;/^OZON_BETWEEN_C/d;/^OZON_ANTI/d' .env
docker compose down
sudo rm -rf app/chrome-profile
docker compose build
docker compose run --rm worker python -m utils.fp_check | grep -E "FAIL|ИТОГ"
docker compose up -d
```

## Тесты

```bash
source .venv/bin/activate
python -m pytest tests -q
```

Путь `tests` обязателен: без него pytest обходит и `app/chrome-profile`, созданный контейнером от root.