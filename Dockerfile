FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PIXIV_DB_PATH=/app/data/state/pixiv_sync.db \
    PIXIV_PUBLIC_DIR=/app/data/library/public \
    PIXIV_PRIVATE_DIR=/app/data/library/private

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 pixiv \
    && useradd --uid 1000 --gid pixiv --create-home pixiv

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip install .

# 自动登录/刷新 Cookie 才需要浏览器；默认镜像可手动粘贴 Token 和 Cookie。
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
      python -m playwright install --with-deps chromium \
      && chmod -R a+rX /opt/playwright \
      && rm -rf /var/lib/apt/lists/*; \
    fi

RUN mkdir -p /app/config /app/data \
    && chown pixiv:pixiv /app/config /app/data
USER pixiv
EXPOSE 5010
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import json, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:5010/api/health', timeout=3); assert r.status == 200 and json.load(r)['status'] == 'ok'"]
ENTRYPOINT ["python", "-m", "pixiv_novel_sync.container"]
CMD ["web-token-ui", "--host", "0.0.0.0", "--port", "5010"]
