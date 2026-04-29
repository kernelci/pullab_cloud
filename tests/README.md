# Testing

Unit and integration tests using pytest.

## Quick Reference

```bash
# Unit tests only (no AWS resources)
make test

# Linting (flake8 + pylint)
make lint

# Format code (black + isort)
make format

# Unit tests with coverage
pytest tests/ -v --cov=src --cov-report=term-missing -m "not integration"

# Integration tests (spawns real AWS resources, incurs costs)
pytest tests/integration/ -v -m integration
```

All commands should be run in the virtual environment (`source .venv/bin/activate` or via `tests/test-in-venv.sh`).

## Structure

- `tests/` — Unit tests (mocked, no AWS access needed)
- `tests/integration/` — Integration tests (real AWS resources)
- `tests/conftest.py` — Shared fixtures
- `tests/test-in-venv.sh` — Wrapper that creates/activates venv and runs commands

## Dependencies

Install with: `pip install -e ".[dev]"`

Includes: pytest, pytest-cov, black, isort, flake8, pylint, pre-commit
