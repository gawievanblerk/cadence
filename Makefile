.PHONY: install test demo demo-swap demo-content api walkthrough seed-demo docker-up docker-down validate lint clean

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

# Serves the API plus the three reference interfaces at /ui/, /ui/author, /ui/admin
api:
	uvicorn cadence.api.main:app --reload --port 8000

# Seeds a caseload, drives the three interfaces, and writes a captioned mp4 to
# var/. Regenerating it from the current code is the point: the video cannot
# drift from what the system does.
walkthrough:
	python scripts/record_walkthrough.py

# Three scripted clients into whatever CADENCE_DATABASE_URL points at, so the
# review screen has a caseload. --reset drops every table first. Demo only.
seed-demo:
	python scripts/seed_demo.py --reset

# The same stack the test server runs, on this machine: Postgres plus the app
# on http://127.0.0.1:8000. docker-compose.prod.yml adds Caddy for HTTPS.
docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

validate:
	python -m cadence.cli content validate

lint:
	ruff check src tests scripts

clean:
	rm -rf var .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
