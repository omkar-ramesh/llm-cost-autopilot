.PHONY: up down test lint bench migrate

up:
	docker compose up --build -d

down:
	docker compose down

test:
	pytest -q

lint:
	ruff check app tests

bench:
	python scripts/benchmark.py $(BENCH_ARGS)

migrate:
	python -c "from app.db import init_db; init_db()"
