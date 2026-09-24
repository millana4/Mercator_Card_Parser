# Mercator Card Parser

Сервис парсинга карточек маркетплейсов. Умеет подбирать карточки под страту, например: Рубашки женские, 4 шт. 
А также умеет парсить данные карточек: на вход принимает URL или артикул, на выход отдает
сырой JSON `__NUXT__` или очищенные данные карточки согласно модели данных.

Сейчас поддерживает только Ozon.

Работать с сервисом можно двумя способами: синхронно через HTTP (Swagger) или
асинхронно через Redis — для интеграции с таск-менеджером.

## Стек технологий

- **Python 3.13**
- **FastAPI** — HTTP-API - Swagger на `/docs`
- **Selenium + selenium-stealth** — управляемый браузер с обходом антибота
- **Google Chrome (headless) + Xvfb** — рендеринг страниц Ozon
- **Pydantic / pydantic-settings** — модели данных и настройки
- **redis** — асинхронный транспорт задач (общий пул через списки)
- **Docker + Docker Compose** — упаковка и запуск (HTTP-сервис + воркер)
- **pytest** — тесты маппера, подбора и воркера

## Структура приложения
```
app/
├── main.py                  # точка входа FastAPI
├── api/
│   ├── __init__.py
│   ├── dependencies.py      # проверка ключа между сервисами
│   └── routes/              # HTTP-эндпоинты (свёрнуто на скриншоте)
├── categories/              # категории товаров и их конфигурация
│   ├── __init__.py
│   ├── base.py              # базовый класс категории
│   ├── clothing.py          # категория «одежда»
│   ├── kids_clothing.py     # категория «детская одежда»
│   ├── models.py            # модели категорий
│   └── registry.py          # реестр категорий
├── core/
│   ├── __init__.py
│   ├── config.py            # настройки из env + политики таймаутов/пауз/ретраев per-маркетплейс
│   ├── exceptions.py        # доменные ошибки (антибот / нет в наличии / не найдено / невалидный вход)
│   ├── logging.py           # уровни логирования
│   └── security.py          # декодирование токена
├── chrome-profile/          # профиль Chrome (куки, история — для «человечного» отпечатка)
├── debug/                   # debug-артефакты: html, json, скриншоты
├── marketplaces/
│   ├── __init__.py
│   └── ozon/
│       ├── __init__.py
│       ├── parser.py         # Selenium: открыть страницу/выдачу, получить HTML, детект антибота/out_of_stock
│       ├── mapper.py         # сырьё -> доменная карточка OzonCard, определение button_state
│       ├── models.py         # доменные модели (OzonCard и др.)
│       ├── nuxt.py           # извлечение __NUXT__ из HTML
│       ├── repository.py     # источник данных: url/id/запрос -> сырьё
│       ├── schemas.py        # схемы API (запрос/ответ)
│       ├── search_listing.py # выдача: извлечение кандидатов, отсев, пагинация
│       ├── selection.py      # подбор под страту: потребность, сезон, оркестрация
│       └── service.py        # оркестрация парсинга + debug-файлы
├── shared/
│   ├── __init__.py
│   └── logger.py            # лог событий
└── worker/                  # Redis-транспорт: консьюмер задач + продюсер результатов
    ├── __init__.py
    └── ...                  # (раскрой папку — опиши реальные файлы)
tests/                       # тесты
utils/                       # вспомогательные скрипты
.env                         # настройки окружения (не в git)
.env.example                 # шаблон настроек
docker-compose.yml           # сборка и запуск (worker + parsing-service)
Dockerfile
entrypoint.sh                # запуск Xvfb + воркера
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
8. `POST /api/v1/ozon/search/diagnostics` — диагностика поисковой выдачи (служебный)


Файлы debug и служебные эндпоинты:
- `GET /api/v1/files`
- `GET /api/v1/files/{name}`
- `GET /api/v1/health`
- `GET /api/v1/health/ready`

Машиночитаемый контракт API: `http://localhost:8010/openapi.json`

## Развёртывание нового экземпляра парсера

Инструкция по запуску ещё одного парсера на новом сервере. Каждый парсер — отдельный сервер со своим внешним IP; все они читают задачи из общего Redis на сервере ядра и пишут туда результаты.

### 1. Установить Docker

Если на сервере ещё нет Docker, установить его и добавить пользователя в группу `docker` (чтобы запускать без `sudo`):

```bash
sudo usermod -aG docker $USER
```

После этого выйти из SSH и зайти снова — группа применяется только при новом входе. Проверить: `docker ps` должен работать без `sudo`.

### 2. Склонировать репозиторий в папку `mercator_parser`

Имя папки задаётся последним аргументом `git clone`:

```bash
cd ~
git clone https://github.com/millana4/Mercator_Card_Parser mercator_parser
cd mercator_parser
```

### 3. Создать и заполнить `.env`

`.env` не хранится в git — его создают на каждом сервере из шаблона:

```bash
cp .env.example .env
nano .env
```

Значения пауз и политики Ozon — **одинаковые на всех парсерах**. Три значения задаются индивидуально под сервер:

- `PARSER_ID` — уникальный номер парсера. Не должен повторяться с другими экземплярами и с уже использованными номерами (иначе счётчики статистики сольются).
- `REDIS_HOST` — внешний IP сервера, где работает Redis (сервер ядра). На удалённом сервере это именно IP, а не `host.docker.internal` (последнее работает только на самой машине с Redis).
- `REDIS_PASSWORD` — пароль Redis, общий для всех (взять из `.env` ядра).

Остальное (`SERVICE_API_KEY`, `PARSER_MARKETPLACES`, `OZON_GEO_*`, паузы `OZON_*`) — как на других парсерах.

### 4. Собрать и запустить

```bash
docker compose up -d --build
```

`--build` собирает образ с текущим кодом. Контейнер поднимется в фоне и подхватит `.env`.

### 5. Проверить, что парсер работает

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep parser
docker logs --tail 30 mercator_parser-worker-1
```

В логах должны появиться:

- `Воркер запущен | parser_id=<номер>` — стартовал с нужным ID;
- `TaskConsumer подключён к Redis` — достучался до Redis;
- `Получена задача` и далее `ok=True` — берёт задачи и успешно парсит.

### 6. Логи в реальном времени

`docker compose logs -f worker`

### 7. Диагностика парсеров

```commandline
c=mercator_parser-worker-1
echo "=== $(docker exec $c env | grep PARSER_ID) ==="
echo "За 24ч: успешно=$(docker logs --since 24h $c 2>&1 | grep -c 'ok=True') | антибот=$(docker logs --since 24h $c 2>&1 | grep -c 'ok=False')"
echo "За 1ч:  успешно=$(docker logs --since 1h $c 2>&1 | grep -c 'ok=True') | антибот=$(docker logs --since 1h $c 2>&1 | grep -c 'ok=False')"
echo "Последний успешный замер:"
docker logs --since 48h $c 2>&1 | grep 'ok=True' | tail -1
echo "Причина последних провалов:"
docker logs --since 6h $c 2>&1 | grep -iE 'капча|antibot|редирект|блокировка' | tail -3

```

## Обновление парсера на сервере

Как накатить новые изменения (код и/или настройки) на уже работающий парсер.

### 1. Зайти на сервер и в папку парсера

```bash
ssh <пользователь>@<адрес_сервера>
cd ~/mercator_parser
```

### 2. Обновить код из git

```bash
git pull origin master
```

Ветка парсера — `master` (не `main`). Если `git pull` открывает окно с сообщением о слиянии (merge) — сохранить и выйти: `Ctrl+O`, `Enter`, `Ctrl+X`.

### 3. Обновить `.env`, если менялись настройки

`.env` не хранится в git и правится на сервере вручную:

```bash
nano ~/mercator_parser/.env
```

Внести изменения, сохранить: `Ctrl+O`, `Enter`, `Ctrl+X`.



### 4. Пересобрать и перезапустить контейнер

```bash
docker compose up -d --build
```


Проверить, что контейнер поднялся и работает:

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep parser
docker logs --tail 30 mercator_parser-worker-1
```



