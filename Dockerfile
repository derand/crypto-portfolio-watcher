# Portfolio watcher. Read-only by design: no keys, no signing, so the image
# needs nothing but Python and four packages.
FROM python:3.12-slim AS base

# tzdata: digest.hour is a *local* hour, and .astimezone() in the digest needs a
# real zoneinfo database. Without it TZ is ignored and the summary lands at 9 UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# uid/gid 1000 is the first non-root user on most Linux distributions, so the
# bind-mounted ./data is writable out of the box. Where `id -u` says otherwise,
# override with PUID/PGID in .env.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -M -d /app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/portfolio.example.yaml ./config/portfolio.example.yaml
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/data \
    && chown -R 1000:1000 /app/data

USER 1000:1000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["watch"]


# Tests only: the image above stays free of pytest. Built by the "dev" profile
# in docker-compose.yml.
FROM base AS dev
USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY pytest.ini ./
COPY tests/ ./tests/
USER 1000:1000
