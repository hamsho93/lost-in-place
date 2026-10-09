.PHONY: setup check test synth image

setup:
	uv sync
	cd infra && uv sync

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest -q
	cd infra && uv run ruff check . && uv run pytest -q

synth:
	cd infra && npx --yes aws-cdk@2 synth --quiet

image:
	docker build -f docker/Dockerfile -t lost-in-place .
