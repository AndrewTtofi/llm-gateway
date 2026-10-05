.PHONY: up down logs test test-live lint fmt reload shell install lock

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f gateway

test:
	pytest -m "not live" -q

test-live:
	pytest -q

lint:
	ruff check app tests && mypy app

fmt:
	ruff format app tests && ruff check --fix app tests

reload:
	curl -s -X POST localhost:8000/admin/reload -H "Authorization: Bearer $$(grep GATEWAY_ADMIN_KEY .env | cut -d= -f2)"

shell:
	docker compose exec gateway bash

install:
	pip install --require-hashes -r requirements-dev.txt

# Re-resolve pins after editing requirements*.in. Add --upgrade to pull newer versions.
PIP_COMPILE = pip-compile --quiet --strip-extras --generate-hashes --allow-unsafe --no-emit-index-url
lock:
	$(PIP_COMPILE) $(ARGS) -o requirements.txt requirements.in
	$(PIP_COMPILE) $(ARGS) -o requirements-dev.txt requirements-dev.in
