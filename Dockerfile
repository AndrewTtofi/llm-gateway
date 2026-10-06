# Pinned by digest as well as tag: a re-pushed tag can't change the image (Dependabot
# updates both).
FROM python:3.14.8-slim@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

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
