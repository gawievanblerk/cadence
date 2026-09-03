.PHONY: install test demo demo-swap api validate lint clean

install:
	pip install -e ".[dev]"

test:
	pytest -q

# Runs a full four-session client journey offline, with no API keys.
demo:
	python scripts/demo_journey.py

# Same client, same content, three different providers. Proves the seam.
demo-swap:
	python scripts/demo_provider_swap.py

# Proves a clinician can change behaviour by editing YAML only.
demo-content:
	python scripts/demo_content_change.py

api:
	uvicorn cadence.api.main:app --reload --port 8000

validate:
	python -m cadence.cli content validate

lint:
	ruff check src tests scripts

clean:
	rm -rf var .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
