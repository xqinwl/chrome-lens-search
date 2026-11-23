FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver ca-certificates xvfb xauth \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 glens \
    && install -d -o glens -g glens /data /data/logs \
    && install -o glens -g glens -m 0755 /usr/bin/chromedriver /home/glens/chromedriver

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY glens ./glens
RUN pip install --no-cache-dir ".[server,browser]"

ENV GLENS_BACKEND=auto \
    GLENS_COOKIE_FILE=/data/cookies.json \
    GLENS_CACHE=1 \
    GLENS_CACHE_DIR=/data/results \
    GLENS_CHROME_PROFILE=/data/chrome-profile \
    GLENS_LOG_LEVEL=INFO \
    GLENS_LOG_FILE=/data/logs/glens.log \
    GLENS_HEADLESS=0 \
    GLENS_PROXY_FALLBACK=1 \
    GLENS_MAX_IMAGE_EDGE=1024 \
    GLENS_RESOLVE_MAX=5 \
    GLENS_CHROMIUM_PATH=/usr/bin/chromium \
    GLENS_CHROMEDRIVER_PATH=/home/glens/chromedriver

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

ENV GLENS_AUTO_WARMUP=1 \
    GLENS_AUTO_WARMUP_INTERVAL=14400

USER glens
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/healthz', timeout=2).close()"]
ENTRYPOINT ["/usr/bin/tini", "-s", "-g", "--", "xvfb-run", "-a"]
CMD ["glens", "serve", "--host", "0.0.0.0", "--port", "8000"]
