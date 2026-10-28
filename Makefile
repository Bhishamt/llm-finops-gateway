.PHONY: install lint fmt type test check run docker

install:
	python -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pre-commit install

lint:
	ruff check . && ruff format --check .

fmt:
	ruff check --fix . && ruff format .

type:
	mypy src tests

test:
	pytest -q

check: lint type test

run:
	uvicorn --factory llm_finops_gateway.main:create_app --port 8080 --reload

docker:
	docker build -t llm-finops-gateway:dev .
