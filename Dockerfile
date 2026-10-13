FROM python:3.13-slim
ARG CHROME_VERSION=152.0.7977.64
ARG SELAWIK_VERSION=1.01

RUN apt-get update && apt-get install -y \
    wget gnupg xvfb unzip curl \
    && rm -rf /var/lib/apt/lists/*

# --- Chrome конкретной версии (прямая ссылка на .deb, не apt-репозиторий) ---
# В apt-репозитории Google лежит только текущая stable-версия, поэтому пин
# через apt невозможен: ставим .deb напрямую, зависимости подтянет apt.
RUN set -eux; \
    curl -fsSL -o /tmp/chrome.deb \
        "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}-1_amd64.deb"; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/chrome.deb; \
    rm -f /tmp/chrome.deb; \
    rm -rf /var/lib/apt/lists/*; \
    google-chrome --version

# --- chromedriver той же версии (вшиваем в образ) ---
RUN set -eux; \
    curl -fsSL -o /tmp/chromedriver.zip \
        "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"; \
    unzip /tmp/chromedriver.zip -d /tmp/; \
    mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver; \
    chmod +x /usr/local/bin/chromedriver; \
    rm -rf /tmp/chromedriver.zip /tmp/chromedriver-linux64; \
    chromedriver --version

# --- Проверка: Chrome и chromedriver должны совпасть по версии ---
RUN set -eux; \
    CHROME_ACTUAL="$(google-chrome --version | grep -oP '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+')"; \
    DRIVER_ACTUAL="$(chromedriver --version | grep -oP '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+')"; \
    echo "Chrome: ${CHROME_ACTUAL}, chromedriver: ${DRIVER_ACTUAL}"; \
    test "${CHROME_ACTUAL}" = "${CHROME_VERSION}"; \
    test "${DRIVER_ACTUAL}" = "${CHROME_VERSION}"

# --- chromedriver: убрать маркеры cdc_ ---
# Chromedriver создаёт в window переменные cdc_adoQpoasnfa76pfcZLmcfl_*, по ним
# антиботы узнают Selenium. Заменяем строку в бинарнике на случайную той же длины
# (своя при каждой сборке). Если исходной строки нет — сменилась версия
# chromedriver, сборка падает: нужно найти новую строку.
RUN set -eux; \
    grep -aq 'cdc_adoQpoasnfa76pfcZLmcfl' /usr/local/bin/chromedriver; \
    NEW_TOKEN="$(tr -dc 'a-z' < /dev/urandom | head -c 26)"; \
    test "${#NEW_TOKEN}" -eq 26; \
    perl -pi -e "s/cdc_adoQpoasnfa76pfcZLmcfl/${NEW_TOKEN}/g" /usr/local/bin/chromedriver; \
    if grep -aq 'cdc_adoQpoasnfa76pfcZLmcfl' /usr/local/bin/chromedriver; then \
        echo "Маркер cdc_ остался в chromedriver"; exit 1; \
    fi; \
    chromedriver --version

# --- Шрифты Windows ---
# Microsoft core fonts (Arial, Times New Roman, Courier New, Verdana, Georgia,
# Trebuchet MS, Comic Sans MS, Impact) лежат в разделе contrib; лицензию
# принимаем заранее. Carlito и Caladea — метрические замены Calibri и Cambria
# (fontconfig Debian связывает их с этими именами).
RUN set -eux; \
    sed -i 's/^Components: main$/Components: main contrib/' /etc/apt/sources.list.d/debian.sources; \
    echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
        | debconf-set-selections; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        fontconfig ttf-mscorefonts-installer fonts-crosextra-carlito fonts-crosextra-caladea; \
    rm -rf /var/lib/apt/lists/*

# Selawik — открытая замена Segoe UI от Microsoft (OFL), метрики Segoe UI.
RUN set -eux; \
    curl -fsSL -o /tmp/selawik.zip \
        "https://github.com/microsoft/Selawik/releases/download/${SELAWIK_VERSION}/Selawik_Release.zip"; \
    mkdir -p /tmp/selawik /usr/share/fonts/truetype/selawik; \
    unzip -q /tmp/selawik.zip -d /tmp/selawik; \
    find /tmp/selawik -iname '*.ttf' -exec cp {} /usr/share/fonts/truetype/selawik/ \; ; \
    rm -rf /tmp/selawik /tmp/selawik.zip; \
    ls /usr/share/fonts/truetype/selawik/*.ttf

# Правила fontconfig: скрыть Linux-шрифты, подставлять Selawik под имя Segoe UI.
COPY docker/fontconfig/*.conf /etc/fonts/conf.d/

# --- Проверка шрифтов: Windows видны под своими именами, Linux скрыты ---
# 30-metric-aliases.conf удаляем: замены Calibri/Cambria/Segoe UI уже названы
# именами Windows, а это правило подставляло Arial на запрос «Liberation Sans».
RUN set -eux; \
    rm -f /etc/fonts/conf.d/30-metric-aliases.conf; \
    fc-cache -f; \
    fc-list : family | grep -qx "Arial"; \
    fc-list : family | grep -qx "Times New Roman"; \
    fc-list : family | grep -qx "Calibri"; \
    fc-list : family | grep -qx "Cambria"; \
    fc-list : family | grep -q "^Segoe UI"; \
    if fc-list : family | grep -qiE "liberation|carlito|caladea"; then \
        echo "Видны шрифты Linux или замены под своими именами"; exit 1; \
    fi; \
    for f in "Segoe UI" Calibri Cambria Arial "Liberation Sans" sans-serif serif monospace; do \
        echo "$f -> $(fc-match "$f")"; \
    done

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/app/debug && chmod +x /app/entrypoint.sh

ENV DISPLAY=:99 \
    CHROME_VERSION=${CHROME_VERSION}
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]