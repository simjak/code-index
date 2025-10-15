# CodeIndex‑JSON

Hierarchy:
- L0 repo
- L1 packages
- L2 files
- L3 classes/functions/constants
- L4 blocks (methods; try/for/while/with regions)

## Install & Run (with `uv`)
```
uv sync
export OPENAI_API_KEY=sk-...    # optional for summaries
uv run codeindex build /path/to/repo --out ./index --summarizer gpt-5-nano
uv run codeindex search --index ./index "where are db writes to orders?" --top 10
uv run codeindex trace --index ./index --open-html
```
