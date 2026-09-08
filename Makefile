.PHONY: up down build config migrate seed test test-integration lint smoke logs

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

config:
	docker compose config

migrate:
	docker compose run --rm api alembic upgrade head

seed:
	docker compose run --rm api python -c "from attention_router.infrastructure.db import SessionLocal; from attention_router.infrastructure.repository import seed_policies; s=SessionLocal(); seed_policies(s); s.commit(); s.close()"

test:
	pytest

test-integration:
	docker compose run --rm api pytest -q -m postgres

test-postgres-contract:
	POSTGRES_TEST_SELECTOR="$(if $(POSTGRES_TEST_SELECTOR),$(POSTGRES_TEST_SELECTOR),tests/integration/test_postgres_10_5c_contract.py -m postgres)" ./scripts/postgres_test_harness.sh

test-postgres-migration-rehearsal:
	POSTGRES_TEST_TARGET=rehearsal POSTGRES_TEST_RUN=0 ./scripts/postgres_test_harness.sh

lint:
	ruff check .

smoke:
	python scripts/smoke.py

logs:
	docker compose logs --tail=120 -f api worker
