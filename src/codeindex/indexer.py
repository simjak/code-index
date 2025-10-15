from __future__ import annotations

import json
import os

from .ast_indexer import PyFileIndexer, stable_id
from .bm25 import BM25Index
from .logger import logger
from .nodes import Node, NodeKind
from .store import write_edges, write_jsonl, write_nodes
from .summarizer import summarize_code
from .ts_indexer import TSFileIndexer

PY_EXTS = {".py"}
JS_TS_EXTS = {".js", ".jsx", ".ts", ".tsx"}

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


def build(
    repo_path: str,
    out_dir: str,
    *,
    summarizer: str = "off",
    min_loc_for_summary: int = 20,
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
    logger.info("Indexing files for supported languages...")
    for dirpath, dirnames, filenames in os.walk(repo_path):
        # Filter out excluded directories in-place to skip traversal
        dirnames[:] = [d for d in dirnames if not should_exclude_dir(d)]

        parent_id = pkg_map.get(dirpath, repo_id)
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            if not is_supported_file(fp):
                continue
            rpath = rel(repo_path, fp)
            logger.debug("Indexing file: %s", rpath)
            try:
                text = open(fp, "r", encoding="utf-8").read()
            except Exception as e:
                logger.warning("Skipping unreadable file %s: %s", rpath, e)
                continue

            # Dispatch by extension
            ext = os.path.splitext(fp)[1].lower()
            if ext in PY_EXTS:
                idx = PyFileIndexer(rpath, text)
            else:
                try:
                    idx = TSFileIndexer(rpath, text)
                except Exception as e:
                    logger.warning("TS/JS parse failed for %s: %s", rpath, e)
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
                    continue

            f_nodes, f_edges = idx.index()
            # fix parent
            f_nodes[0].parent_id = parent_id

            # Summaries
            if summarizer != "off":
                for n in f_nodes:
                    if (
                        n.kind in (NodeKind.FILE, NodeKind.CLASS, NodeKind.FUNC)
                        and (n.loc or 0) >= min_loc_for_summary
                    ):
                        logger.debug(
                            "Summarizing node %s (%s) with %s",
                            n.node_id,
                            n.symbol,
                            summarizer,
                        )
                        snippet = text.splitlines()[
                            (n.start_line or 1) - 1 : (n.end_line or 1)
                        ]
                        n.summary = n.summary or summarize_code(
                            "\n".join(snippet), model=summarizer
                        )

            nodes.extend(f_nodes)
            edges.extend(f_edges)

            for n in f_nodes:
                node_text_rows.append(
                    {
                        "node_id": n.node_id,
                        "text": " ".join(
                            [rpath, n.symbol or "", n.signature or "", n.summary or ""]
                        ),
                    }
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
