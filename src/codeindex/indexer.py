from __future__ import annotations

import asyncio
import json
import os
import time

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from .ast_indexer import DEFAULT_CALLSITE_CAP, PyFileIndexer, stable_id
from .bm25 import BM25Index
from .logger import logger
from .nodes import CallsiteRecord, Node, NodeKind
from .store import write_edges, write_jsonl, write_nodes, write_xref_calls
from .summarizer import summarize_many_async
from .ts_indexer import TSFileIndexer

PY_EXTS = {".py"}
JS_TS_EXTS = {".js", ".jsx", ".ts", ".tsx"}

FEATURE_FLAG_ENV = "CODEINDEX_FEATURE_DOCS_NODES_ENHANCED"
LEGACY_ENRICH_ENV = "CODEINDEX_ENRICH"
CALLSITE_CAP_ENV = "CODEINDEX_CALLSITE_CAP"

# Directories to exclude from indexing (dependencies, caches, build artifacts)
EXCLUDED_DIRS = {
    # Version control
    ".git",
    ".svn",
    ".hg",
    # Python
    "venv",
    ".venv",
    "env",
    ".env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    "*.egg-info",
    ".egg-info",
    "dist",
    "build",
    ".Python",
    # JavaScript/Node
    "node_modules",
    ".npm",
    ".yarn",
    ".pnp",
    # Coverage and testing
    "htmlcov",
    ".coverage",
    "coverage",
    ".nyc_output",
    # IDEs
    ".vscode",
    ".idea",
    ".DS_Store",
    # Other build tools
    "target",  # Rust, Java
    ".gradle",
    ".maven",
    # Misc
    ".cache",
    "tmp",
    "temp",
}


def should_exclude_dir(dirname: str) -> bool:
    """Check if a directory should be excluded from indexing."""
    # Exclude if in the excluded set
    if dirname in EXCLUDED_DIRS:
        return True
    # Exclude if it matches a pattern (e.g., *.egg-info)
    if dirname.endswith(".egg-info") or dirname.endswith("-info"):
        return True
    # Exclude hidden directories (start with .)
    if dirname.startswith("."):
        return True
    return False


def is_supported_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in PY_EXTS or ext in JS_TS_EXTS:
        base = os.path.basename(path)
        return not base.startswith(".")
    return False


def is_python_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PY_EXTS and not os.path.basename(
        path
    ).startswith(".")


def is_js_ts_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in JS_TS_EXTS and not os.path.basename(
        path
    ).startswith(".")


def rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _env_truthy(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_enrichment_enabled() -> bool:
    if os.getenv(FEATURE_FLAG_ENV) is not None:
        return _env_truthy(FEATURE_FLAG_ENV)
    return _env_truthy(LEGACY_ENRICH_ENV)


def _resolve_callsite_cap(default: int = DEFAULT_CALLSITE_CAP) -> int:
    raw = os.getenv(CALLSITE_CAP_ENV)
    if not raw:
        return default
    try:
        cap = int(raw)
    except ValueError:
        logger.warning(
            "WARNING: invalid %s=%s, falling back to %d", CALLSITE_CAP_ENV, raw, default
        )
        return default
    if cap <= 0:
        logger.warning(
            "WARNING: %s must be positive, falling back to %d",
            CALLSITE_CAP_ENV,
            default,
        )
        return default
    return cap


def _compress_text_for_summary(text: str, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.7)]
    tail = text[-int(max_chars * 0.25) :]
    return head.rstrip() + "\n...\n" + tail.lstrip()


def build(
    repo_path: str,
    out_dir: str,
    *,
    summarizer: str = "gpt-5-nano-2025-08-07",
    min_loc_for_summary: int = 20,
    summary_scope: str = "structured",
) -> str:
    repo_path = os.path.abspath(repo_path)
    repo_name = os.path.basename(repo_path)
    logger.info("Indexing repository: %s (%s)", repo_name, repo_path)
    logger.debug(
        "build(): resolved repo_path=%s out_dir=%s summarizer=%s min_loc_for_summary=%s",
        repo_path,
        out_dir,
        summarizer,
        min_loc_for_summary,
    )

    enrich_enabled = _is_enrichment_enabled()
    call_cap = _resolve_callsite_cap()
    if enrich_enabled:
        logger.info(
            "INFO: docs.nodes.enhanced enabled (flag=%s, cap=%d)",
            FEATURE_FLAG_ENV,
            call_cap,
        )

    metrics: dict[str, int] = {
        "funcs_total": 0,
        "funcs_with_params": 0,
        "funcs_with_returns": 0,
        "funcs_with_raises": 0,
        "funcs_with_decorators": 0,
        "raises_extracted_total": 0,
        "callsites_total": 0,
        "callsite_cap_hits": 0,
    }
    callsites: list[CallsiteRecord] = []
    scope_env = os.getenv("CODEINDEX_SUMMARY_SCOPE")
    if scope_env:
        summary_scope = scope_env
    summary_scope = summary_scope.lower()
    if summary_scope not in {"structured", "files", "none"}:
        logger.warning(
            "Unknown summary scope '%s'; falling back to 'structured'",
            summary_scope,
        )
        summary_scope = "structured"
    if summarizer == "off":
        summary_scope = "none"
    logger.debug("Summary scope: %s", summary_scope)
    os.makedirs(out_dir, exist_ok=True)
    nodes: list[Node] = []
    edges: list[dict] = []
    node_text_rows = []  # for search text index

    # L0 repo
    repo_id = stable_id("repo", repo_path, None, None, None)
    nodes.append(
        Node(
            node_id=repo_id,
            parent_id=None,
            kind=NodeKind.REPO,
            path=repo_path,
            symbol=os.path.basename(repo_path),
        )
    )

    # L1 packages: any directory that contains a supported file
    pkg_map: dict[str, str] = {}
    excluded_count = 0
    logger.info("Scanning repository tree for supported packages...")
    for dirpath, dirnames, filenames in os.walk(repo_path):
        # Filter out excluded directories in-place to skip traversal
        excluded = [d for d in dirnames if should_exclude_dir(d)]
        if excluded:
            excluded_count += len(excluded)
            logger.debug(
                "Excluding directories in %s: %s", rel(repo_path, dirpath), excluded
            )
        dirnames[:] = [d for d in dirnames if not should_exclude_dir(d)]

        if any(is_supported_file(os.path.join(dirpath, f)) for f in filenames):
            rid = stable_id("pkg", rel(repo_path, dirpath), None, None, None)
            parent_dir = os.path.dirname(dirpath)
            parent_id = repo_id
            if parent_dir in pkg_map:
                parent_id = pkg_map[parent_dir]
            n = Node(
                node_id=rid,
                parent_id=parent_id,
                kind=NodeKind.PKG,
                path=rel(repo_path, dirpath),
                symbol=os.path.basename(dirpath),
            )
            nodes.append(n)
            pkg_map[dirpath] = rid
            logger.debug("Registered package node: %s -> %s", dirpath, rid)

    if excluded_count > 0:
        logger.info(
            "Excluded %d directories (dependencies, caches, build artifacts)",
            excluded_count,
        )

    # L2/L3/L4 for all supported languages
    # First pass: count files to show accurate progress
    logger.info("Scanning files for progress tracking...")
    files_to_index = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if not should_exclude_dir(d)]
        parent_id = pkg_map.get(dirpath, repo_id)
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            if is_supported_file(fp):
                files_to_index.append((fp, parent_id))

    logger.info("Indexing %d files...", len(files_to_index))

    # Phase 1: Index all files and collect nodes (fast)
    # Phase 2: Batch summarize all nodes at once (parallel)
    console = Console()

    # Data structures to accumulate work
    file_data_list = []  # List of (rpath, text, f_nodes, f_edges, f_calls, f_stats)
    global_summary_work = []  # List of (file_idx, node_idx, node, snippet) for summarization

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        # Phase 1: Index all files quickly
        task = progress.add_task(
            "[cyan]Phase 1: Indexing files", total=len(files_to_index)
        )

        for file_idx, (fp, parent_id) in enumerate(files_to_index):
            rpath = rel(repo_path, fp)
            # Shorten path for display
            short_path = rpath if len(rpath) < 50 else "..." + rpath[-47:]
            progress.update(task, description=f"[cyan]{short_path}")
            try:
                text = open(fp, "r", encoding="utf-8").read()
            except Exception as e:
                logger.warning("Skipping unreadable file %s: %s", rpath, e)
                logger.trace("TRACE: skipped %s due to unreadable file", rpath)
                progress.advance(task)
                continue

            # Dispatch by extension
            ext = os.path.splitext(fp)[1].lower()
            if ext in PY_EXTS:
                idx = PyFileIndexer(
                    rpath, text, enrich=enrich_enabled, call_cap=call_cap
                )
                lang_label = "python"
            else:
                try:
                    idx = TSFileIndexer(
                        rpath, text, enrich=enrich_enabled, call_cap=call_cap
                    )
                    lang_label = (
                        "javascript" if ext in {".js", ".jsx"} else "typescript"
                    )
                except Exception as e:
                    logger.warning("TS/JS parse failed for %s: %s", rpath, e)
                    logger.trace("TRACE: skipped %s due to ts/JS parse failure", rpath)
                    # Fallback: treat as a file node only
                    n = Node(
                        node_id=stable_id(
                            "file", rpath, None, 1, len(text.splitlines())
                        ),
                        parent_id=parent_id,
                        kind=NodeKind.FILE,
                        path=rpath,
                        lang="javascript" if ext in {".js", ".jsx"} else "typescript",
                        symbol=os.path.basename(rpath),
                        start_line=1,
                        end_line=len(text.splitlines()),
                        loc=len(text.splitlines()),
                    )
                    nodes.append(n)
                    node_text_rows.append(
                        {
                            "node_id": n.node_id,
                            "text": " ".join([rpath, n.symbol or ""]),
                        }
                    )
                    progress.advance(task)
                    continue

            start_time = time.perf_counter()
            try:
                f_nodes, f_edges, f_calls, f_stats = idx.index()
            except SyntaxError as exc:
                logger.warning("Skipping %s due to syntax error: %s", rpath, exc)
                logger.trace("TRACE: skipped %s due to syntax error", rpath)
                progress.advance(task)
                continue
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.trace(
                "TRACE: indexed %s (lang=%s nodes=%d edges=%d calls=%d) in %.1fms",
                rpath,
                lang_label,
                len(f_nodes),
                len(f_edges),
                len(f_calls),
                duration_ms,
            )
            # fix parent
            f_nodes[0].parent_id = parent_id

            # Store file data for Phase 2
            file_data_list.append(
                (rpath, text, f_nodes, f_edges, f_calls, f_stats, parent_id)
            )

            # Collect nodes that need summarization (don't summarize yet)
            if summarizer != "off" and summary_scope != "none":
                lines = text.splitlines() if summary_scope == "structured" else None
                for node_idx, n in enumerate(f_nodes):
                    if n.summary:
                        continue
                    if summary_scope == "files":
                        if (
                            n.kind != NodeKind.FILE
                            or (n.loc or 0) < min_loc_for_summary
                        ):
                            continue
                        snippet = _compress_text_for_summary(text)
                        global_summary_work.append((file_idx, node_idx, n, snippet))
                        break  # only need the file-level summary
                    else:  # structured
                        if (
                            n.kind in (NodeKind.FILE, NodeKind.CLASS, NodeKind.FUNC)
                            and (n.loc or 0) >= min_loc_for_summary
                        ):
                            snippet_lines = lines[
                                (n.start_line or 1) - 1 : (n.end_line or 1)
                            ]  # type: ignore[index]
                            global_summary_work.append(
                                (file_idx, node_idx, n, "\n".join(snippet_lines))
                            )

            # Update progress bar
            progress.advance(task)

        # Phase 2: Batch summarize ALL nodes at once
        if global_summary_work and summarizer != "off":
            conc = int(
                os.getenv("CODEINDEX_SUMMARY_CONCURRENCY", "50")
            )  # Higher default for global batch
            logger.info(
                "Phase 2: Batch summarizing %d nodes across all files with concurrency=%d",
                len(global_summary_work),
                conc,
            )

            task2 = progress.add_task(
                "[yellow]Phase 2: Batch summarizing", total=len(global_summary_work)
            )

            # Extract texts for summarization
            texts = [work[3] for work in global_summary_work]

            try:
                summaries = asyncio.run(
                    summarize_many_async(texts, model=summarizer, concurrency=conc)
                )
                success_count = sum(1 for s in summaries if s is not None)
                logger.info(
                    "Batch summarization complete: %d/%d succeeded",
                    success_count,
                    len(global_summary_work),
                )

                # Assign summaries back to nodes
                for (file_idx, node_idx, node, _), summary in zip(
                    global_summary_work, summaries
                ):
                    if summary:
                        node.summary = summary
                    progress.advance(task2)

            except Exception as e:
                logger.error(
                    "Batch summarization failed: %s: %s. Skipping all summaries.",
                    type(e).__name__,
                    e,
                )

        # Phase 3: Consolidate all data
        logger.info("Phase 3: Consolidating results...")
        task3 = progress.add_task(
            "[green]Phase 3: Consolidating", total=len(file_data_list)
        )

        for (
            rpath,
            text,
            f_nodes,
            f_edges,
            f_calls,
            f_stats,
            parent_id,
        ) in file_data_list:
            nodes.extend(f_nodes)
            edges.extend(f_edges)
            if f_calls:
                callsites.extend(f_calls)
            for key, value in f_stats.items():
                metrics[key] = metrics.get(key, 0) + value

            for n in f_nodes:
                node_text_rows.append(
                    {
                        "node_id": n.node_id,
                        "text": " ".join(
                            [rpath, n.symbol or "", n.signature or "", n.summary or ""]
                        ),
                    }
                )
            progress.advance(task3)

    if enrich_enabled:
        logger.info(
            "INFO: funcs_with_params_total=%d funcs_total=%d funcs_with_returns_total=%d funcs_with_raises_total=%d raises_extracted_total=%d",
            metrics["funcs_with_params"],
            metrics["funcs_total"],
            metrics["funcs_with_returns"],
            metrics["funcs_with_raises"],
            metrics["raises_extracted_total"],
        )
        logger.info(
            "INFO: callsites_written_total=%d callsite_cap_hits=%d",
            metrics["callsites_total"],
            metrics["callsite_cap_hits"],
        )
        if metrics["funcs_total"] > 0:
            ratio = metrics["funcs_with_params"] / max(metrics["funcs_total"], 1)
            if ratio < 0.6:
                logger.warning(
                    "ALERT: funcs_with_params_total/funcs_total dropped to %.2f", ratio
                )
        if metrics["funcs_total"] > 100 and metrics["callsites_total"] == 0:
            logger.warning(
                "ALERT: callsites_written_total == 0 for repo with %d funcs",
                metrics["funcs_total"],
            )

    # Persist

    logger.info(
        "Persisting artifacts: %d nodes, %d edges, %d node text rows",
        len(nodes),
        len(edges),
        len(node_text_rows),
    )
    write_nodes(os.path.join(out_dir, "nodes.jsonl"), nodes)
    write_edges(os.path.join(out_dir, "edges.jsonl"), edges)
    write_jsonl(os.path.join(out_dir, "node_texts.jsonl"), node_text_rows)
    if enrich_enabled:
        write_xref_calls(os.path.join(out_dir, "xref_calls.jsonl"), callsites)

    # BM25
    bm25 = BM25Index()
    for row in node_text_rows:
        bm25.add_doc(row["node_id"], row["text"])
    bm25.finalize()
    bm25.save(os.path.join(out_dir, "bm25.json"))
    logger.debug("BM25 index persisted to %s", os.path.join(out_dir, "bm25.json"))

    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"repo_id": repo_id, "created": True, "langs": ["python", "js", "ts"]}, f
        )

    logger.info("Build artifacts written to %s", out_dir)
    return out_dir
