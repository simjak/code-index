# CodeIndex‑JSON

Hierarchy:
- L0 repo
- L1 packages
- L2 files
- L3 classes/functions/constants
- L4 blocks (methods; try/for/while/with regions)

## Install & Run (with `uv`)

### Basic Usage (no summaries)
```bash
uv sync
uv run codeindex build /path/to/repo --out ./index
uv run codeindex search --index ./index "where are db writes to orders?" --top 10
uv run codeindex trace --index ./index --open-html
```

> **💡 Progress Bars**: The `build` command now shows real-time progress with:
> - **Phase 1**: File indexing progress (X/Y files processed)
> - **Phase 2**: Batch summarization with high parallelism
> - **Phase 3**: Data consolidation
> - Estimated time remaining for each phase
>
> **⚡ Performance**: Optimized three-phase architecture provides **10-20x faster** indexing with summarization compared to v0.1.
> - 280 files: From ~11 minutes to ~45 seconds
> - All nodes summarized in parallel (concurrency=50 by default)

### With AI Summaries (optional)
```bash
# 1. Set your OpenAI API key
export OPENAI_API_KEY=sk-...

# 2. Test your connection (recommended first time)
uv run python test_openai_connection.py gpt-4o-mini

# 3. Build with summaries (recommended: gpt-4o-mini for speed/cost)
uv run codeindex build /path/to/repo --out ./index --summarizer gpt-4o-mini

# 4. Search and view trace
uv run codeindex search --index ./index "where are db writes to orders?" --top 10
uv run codeindex trace --index ./index --open-html
```

### Environment Variables (optional)
- `OPENAI_API_KEY`: Your OpenAI API key (required for `--summarizer`)
- `CODEINDEX_SUMMARY_TIMEOUT`: Request timeout in seconds (default: 30)
- `CODEINDEX_SUMMARY_RETRIES`: Max retry attempts on failure (default: 2)
- `CODEINDEX_SUMMARY_CONCURRENCY`: Max parallel API requests (default: 50, raised from 5 in v0.2 for better performance)
- `CODEINDEX_FEATURE_DOCS_NODES_ENHANCED`: Enable enriched node metadata + callsite capture (`1` to enable; defaults off)
- `CODEINDEX_ENRICH`: Legacy toggle for enrichment (falls back when feature flag env is unset)
- `CODEINDEX_CALLSITE_CAP`: Maximum callsites to retain per function (default: 200)

### Recommended Models
- **gpt-4o-mini**: Fast, cheap, good quality (recommended for most users)
- **gpt-4o**: Higher quality, slower, more expensive
- **gpt-3.5-turbo**: Legacy model, cheapest option

## Documentation Artifacts

When enrichment is enabled, the build produces richer metadata for auto-documentation:

- `nodes.jsonl`: Each `Node` now stores doc-friendly attributes under `extra.doc`. For Python functions this includes parameters (name/type/default/kind), return annotations, raises, decorators, async/method flags, and owner class. TS/JS functions include parameter names plus async/generator flags.
- `xref_calls.jsonl`: New JSONL file capturing callsites with `caller_id`, `callee_ref` (resolved node id or unresolved symbol + reason), source file, line number, and snippet. Respect the `CODEINDEX_CALLSITE_CAP` limit per function.

The stub doc writer (`codeindex.doc_agent`) consumes the metadata to emit `Args`, `Returns`, and `Raises` sections, falling back to summaries when enrichment is disabled.
