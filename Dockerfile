# Pinned by digest as well as tag: a re-pushed tag can't change the image (Dependabot
# updates both).
FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Debian security fixes that came out after the base image was built.
RUN apt-get update \
    && apt-get -y --no-install-recommends upgrade \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# pip is only needed to install: it's removed afterwards, together with the copies of
# urllib3 and msgpack it bundles, so the image only ships what the gateway runs.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && pip uninstall -y pip

COPY app ./app
COPY config ./config
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd -m gateway
USER gateway

EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
# Apply DB migrations, then serve. (In a multi-replica deploy, run migrations as a
# separate job instead.)
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 30"]
