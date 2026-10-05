.PHONY: up down logs test test-e2e test-live lint fmt reload shell install lock migrate key

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f gateway

test:
	pytest -m "not live and not e2e" -q

test-e2e:
	pytest -m "e2e and not live" -q

test-live:
	pytest -q

lint:
	ruff check app tests && mypy app

fmt:
	ruff format app tests && ruff check --fix app tests

reload:
	@sed -n 's/^GATEWAY_ADMIN_KEY=/Authorization: Bearer /p' .env | curl -s -X POST localhost:8000/admin/reload -H @-; echo

shell:
	docker compose exec gateway bash

install:
	pip install --require-hashes -r requirements-dev.txt

# Re-resolve pins after editing requirements*.in. Add --upgrade to pull newer versions.
PIP_COMPILE = pip-compile --quiet --strip-extras --generate-hashes --allow-unsafe --no-emit-index-url
lock:
	$(PIP_COMPILE) $(ARGS) -o requirements.txt requirements.in
	$(PIP_COMPILE) $(ARGS) -o requirements-dev.txt requirements-dev.in

migrate:
	docker compose exec gateway alembic upgrade head

# Create a gateway API key:  make key name=my-app tier=dev
key:
	@# The admin key goes to curl on stdin (-H @-), never on the command line where `ps` shows it.
	@sed -n 's/^GATEWAY_ADMIN_KEY=/Authorization: Bearer /p' .env | curl -s -X POST localhost:8000/admin/keys \
	  -H @- -H 'content-type: application/json' -d '{"name":"$(name)","tier":"$(or $(tier),dev)"}'; echo
