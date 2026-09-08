FROM python:3.13-slim

# Версия Chrome и chromedriver фиксирована: отпечаток браузера влияет на
# антибот Ozon, поэтому версия не должна меняться при пересборке образа.
# Менять только осознанно, здесь — в одном месте.
ARG CHROME_VERSION=151.0.7922.169

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

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/app/debug && chmod +x /app/entrypoint.sh

ENV DISPLAY=:99
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]