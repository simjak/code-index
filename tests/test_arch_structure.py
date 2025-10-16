from __future__ import annotations

import json
import os
from pathlib import Path

from codeindex.arch import ArchConfig, generate_architecture


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _node(
    node_id: str,
    *,
    parent_id: str | None,
    kind: str,
    path: str,
    symbol: str | None = None,
    loc: int | None = None,
    summary: str | None = None,
) -> dict:
    return {
        "node_id": node_id,
        "parent_id": parent_id,
        "kind": kind,
        "path": path,
        "lang": "python",
        "symbol": symbol,
        "signature": None,
        "start_line": None,
        "end_line": None,
        "loc": loc,
        "summary": summary,
        "hash": None,
        "extra": {},
    }


def test_generate_architecture_outputs(tmp_path):
    index_dir = tmp_path / "index"
    arch_dir = tmp_path / "arch"
    index_dir.mkdir()
    repo_id = "repo"
    pkg_codeindex = "pkg-codeindex"
    pkg_utils = "pkg-utils"

    nodes = [
        _node(repo_id, parent_id=None, kind="repo", path="/tmp/repo", symbol="repo"),
        _node(pkg_codeindex, parent_id=repo_id, kind="pkg", path="src/codeindex", symbol="codeindex"),
        _node(pkg_utils, parent_id=repo_id, kind="pkg", path="src/utils", symbol="utils"),
        _node(
            "file-cli",
            parent_id=pkg_codeindex,
            kind="file",
            path="src/codeindex/cli.py",
            symbol="cli.py",
            loc=120,
            summary="CLI entrypoint.",
        ),
        _node(
            "file-indexer",
            parent_id=pkg_codeindex,
            kind="file",
            path="src/codeindex/indexer.py",
            symbol="indexer.py",
            loc=200,
            summary="Indexer logic.",
        ),
        _node(
            "file-searcher",
            parent_id=pkg_codeindex,
            kind="file",
            path="src/codeindex/searcher.py",
            symbol="searcher.py",
            loc=180,
            summary="Search orchestration.",
        ),
        _node(
            "file-utils",
            parent_id=pkg_utils,
            kind="file",
            path="src/utils/helper.py",
            symbol="helper.py",
            loc=42,
            summary="Helper utilities.",
        ),
        _node(
            "func-main",
            parent_id="file-cli",
            kind="func",
            path="src/codeindex/cli.py",
            symbol="main",
            loc=20,
            summary=None,
        ),
    ]
    edges = [
        {"src": "file-cli", "dst": "utils.helper", "type": "import", "detail": "helper"},
    ]
    _write_jsonl(index_dir / "nodes.jsonl", nodes)
    _write_jsonl(index_dir / "edges.jsonl", edges)

    os.environ["CODEINDEX_LLM_STUB"] = "1"
    config = ArchConfig(
        index_dir=index_dir,
        out_dir=arch_dir,
        llm_model="stub-model",
        max_tokens=512,
        temperature=0.0,
        stub=True,
    )
    generate_architecture(config)

    meta_path = arch_dir / "meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["component_count"] == 2

    index_doc = arch_dir / "docs" / "index.md"
    assert index_doc.exists()
    index_text = index_doc.read_text(encoding="utf-8")
    assert "Repository Overview" in index_text
    assert "codeindex" in index_text

    component_doc = arch_dir / "docs" / "components" / "codeindex.md"
    assert component_doc.exists()
    doc_text = component_doc.read_text(encoding="utf-8")
    assert "Component: codeindex" in doc_text
    assert "Depends on" in doc_text

    diagram_path = arch_dir / "diagrams" / "codeindex.mmd"
    assert diagram_path.exists()
    diagram_text = diagram_path.read_text(encoding="utf-8")
    assert "graph TD" in diagram_text
    assert "codeindex" in diagram_text

    # ensure utilities component also documented
    utils_doc = arch_dir / "docs" / "components" / "utils.md"
    assert utils_doc.exists()
    utils_diagram = arch_dir / "diagrams" / "utils.mmd"
    assert utils_diagram.exists()
    os.environ.pop("CODEINDEX_LLM_STUB", None)
