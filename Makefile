.PHONY: help install install-dev test lint format type-check clean build release docs docs-clean poller poller-once

CONFIG ?= examples/aws/config.json

help:
	@echo "Available targets:"
	@echo "  install      - Install the package"
	@echo "  install-dev  - Install the package with development dependencies"
	@echo "  test         - Run tests"
	@echo "  lint         - Run linting (flake8, pylint)"
	@echo "  format       - Format code (black, isort)"
	@echo "  type-check   - Run type checking (mypy)"
	@echo "  docs         - Build documentation"
	@echo "  docs-clean   - Clean documentation build"
	@echo "  clean        - Clean build artifacts"
	@echo "  build        - Build the package"
	@echo "  release      - Build and check the package for release"
	@echo "  poller       - Run the kernelci pull-lab poller (long-lived)"
	@echo "  poller-once  - Run a single poll cycle and exit"

poller:
	PYTHONPATH=src$${PYTHONPATH:+:$$PYTHONPATH} python -m kernel_ci_cloud_labs.pull_labs_poller --config $(CONFIG)

poller-once:
	PYTHONPATH=src$${PYTHONPATH:+:$$PYTHONPATH} python -m kernel_ci_cloud_labs.pull_labs_poller --config $(CONFIG) --once

install: build test
	python3.11 -m pip install -e .

install-dev:
	python3.11 -m pip install -e ".[dev,test]"

test:
	python -m pytest tests/ -m "not integration"

lint:
	python -m flake8 src tests
	python -m pylint src tests

format:
	python -m black src tests
	python -m isort --profile black src tests

type-check:
	python -m mypy src

docs: $(shell find src -name '*.py') doc/*.rst doc/*.py
	sphinx-build -b html doc doc/_build/html

docs-clean:
	rm -rf doc/_build/
	rm -rf doc/_apidoc/

clean: docs-clean
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf htmlcov/
	rm -f coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

build:
	python -m build

release: clean build test
	python -m twine check dist/*
	@echo "Package is ready for release. Run 'python -m twine upload dist/*' to publish."
