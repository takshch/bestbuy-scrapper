# syntax=docker/dockerfile:1
# linux/amd64 only: Google publishes no linux-arm64 ChromeDriver for CloakBrowser to pair with.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser \
    SE_CACHE_PATH=/opt/selenium \
    SCRAPER_HEADLESS=true

WORKDIR /app

COPY requirements.txt .
# Playwright is installed with cloakbrowser; its install-deps pulls the exact
# system libraries (nss, gtk, fonts, ...) Chromium needs on Debian.
RUN pip install -r requirements.txt \
 && playwright install-deps chromium \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Bake the CloakBrowser binary and matching ChromeDriver into the image.
COPY docker/prefetch_browser.py /tmp/prefetch_browser.py
RUN python /tmp/prefetch_browser.py && rm /tmp/prefetch_browser.py

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN useradd --create-home --uid 10001 scraper \
 && chown -R scraper:scraper /opt/cloakbrowser /opt/selenium
USER scraper

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Scraping runs in separate worker containers (python -m app.worker); the API only queues work.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
