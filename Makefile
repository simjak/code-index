format:
	uv run ruff format ./src && uv run ruff check ./src --fix

lint:
	uv run ruff check ./src

test:
	pytest tests