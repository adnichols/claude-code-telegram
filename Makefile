.PHONY: install dev test lint format clean help run

# Default target
help:
	@echo "Available commands:"
	@echo "  install    - Install production dependencies"
	@echo "  dev        - Install development dependencies"
	@echo "  test       - Run tests"
	@echo "  lint       - Run linting checks"
	@echo "  format     - Format code"
	@echo "  clean      - Clean up generated files"
	@echo "  run        - Run the bot"

install:
	poetry install --no-dev

dev:
	poetry install

test:
	poetry run pytest

lint:
	poetry run black --check src tests
	poetry run isort --check-only src tests
	poetry run flake8 src tests
	poetry run mypy src

format:
	poetry run black src tests
	poetry run isort src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/

run:
	poetry run claude-telegram-bot

# For debugging
run-debug:
	poetry run claude-telegram-bot --debug