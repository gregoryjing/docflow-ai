.PHONY: install install-ocr format lint typecheck test test-integration doctor serve

install:
	python -m pip install -e ".[dev]"

install-ocr:
	python -m pip install -e ".[dev,ocr]"

format:
	ruff format src tests
	ruff check --fix src tests

lint:
	ruff check src tests
	ruff format --check src tests

typecheck:
	mypy

test:
	pytest

test-integration:
	pytest -m integration

doctor:
	docflow doctor

serve:
	docflow serve
