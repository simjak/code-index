You are a self improving system. 
Every time you interact with the user and learn something, write down your insights. 
Feel free to build and tools that you feel will speed up your work.

# Repository Guidelines

## Project Structure & Module Organization
- Source code lives under `src/codeindex`, with modules such as `cli.py` for the entrypoint, `indexer.py` for repository ingestion, and `searcher.py` for query execution.
- Data structures (nodes, BM25 helpers) sit beside the core modules; keep additions single-responsibility to retain low coupling.
- Generated artifacts (JSON index, trace HTML) should be written outside the repo (e.g., `./index/`) to keep the tree clean.

## Build, Test, and Development Commands
- `uv sync` installs Python 3.12 dependencies defined in `pyproject.toml` and `uv.lock`.
- `uv run codeindex build /path/to/repo --out ./index` builds a multi-resolution index; add `--summarizer gpt-5-nano` after exporting `OPENAI_API_KEY` when summaries are needed.
- `uv run codeindex search --index ./index "where are db writes?" --top 10` queries the index and prints results plus trace paths.
- `uv run codeindex trace --index ./index --open-html` regenerates the TraceView bundle and opens it in a browser when available.
- `make format`, `make lint`, `make test` wrap Ruff formatting/checks and `pytest`; run them before pushing.

## Coding Style & Naming Conventions
- Python style is enforced through `ruff format` (PEP 8 compatible) and `ruff check`; prefer four-space indents and type hints for new public APIs.
- Modules and functions follow `snake_case`; classes use `PascalCase`; constants stay upper snake.
- Co-locate CLI parameter parsing in `cli.py` and domain logic in dedicated modules to maintain STTCPW boundaries.

## Testing Guidelines
- Place tests in `tests/` mirroring the module structure (`tests/test_searcher.py`, etc.); name files `test_*.py` and use pytest fixtures for sample repositories.
- Run `uv run pytest tests -k "focus"` while iterating, then `make test` for full suites. Favor real repository samples when reproducing bugs to validate index output shapes.
- Add regression tests whenever fixing bugs that impact indexing, search ranking, or trace generation.

## Commit & Pull Request Guidelines
- Use Conventional Commits with scopes tied to modules (`fix(searcher): guard empty corpus`).
- Each PR should describe expected vs. actual behavior, link relevant issues, and note any schema or CLI changes.
- Before requesting review, ensure `make format`, `make lint`, and `make test` all succeed locally and attach screenshots or sample outputs for trace/CLI changes.

## Security & Configuration Tips
- Keep API keys (e.g., `OPENAI_API_KEY`) in your shell env or `.env` excluded from git; never hard-code credentials.
- Review `.env` usage and redact sensitive fields in logs or PR snippets, especially when sharing TraceView artifacts.
