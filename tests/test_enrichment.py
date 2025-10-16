from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from codeindex.ast_indexer import PyFileIndexer
from codeindex.doc_agent import render_node_doc
from codeindex.indexer import build
from codeindex.nodes import Node, NodeKind
from codeindex.store import load_jsonl
from codeindex.ts_indexer import TSFileIndexer


def _node_by_symbol(nodes, name, kind):
    for node in nodes:
        if node.symbol == name and node.kind == kind:
            return node
    raise AssertionError(f"Node {name!r} with kind {kind} not found")


def test_python_params_enrichment():
    source = """
def sample(a: int, b=5, *, c: str = "x", **kwargs) -> str:
    '''Doc for sample'''
    return str(a + len(kwargs))
"""
    indexer = PyFileIndexer("mod.py", source, enrich=True, call_cap=50)
    nodes, edges, callsites, stats = indexer.index()

    func = _node_by_symbol(nodes, "sample", NodeKind.FUNC)
    meta = func.extra["doc"]
    assert func.signature == "(a: int, b = 5, *, c: str = 'x', **kwargs)"
    assert meta["params"][0]["name"] == "a"
    assert meta["params"][0]["annotation"] == "int"
    assert meta["params"][1]["default"] == "5"
    assert meta["params"][3]["name"] == "kwargs"
    assert meta["returns"] == "str"
    assert stats["funcs_with_params"] == 1


def test_python_raises_extraction():
    source = """
class CustomError(Exception):
    pass


def boom(flag: bool) -> None:
    if flag:
        raise ValueError("bad flag")
    try:
        risky()
    except RuntimeError:
        raise CustomError("wrapped")
"""
    indexer = PyFileIndexer("errors.py", source, enrich=True, call_cap=50)
    nodes, _, _, stats = indexer.index()
    func = _node_by_symbol(nodes, "boom", NodeKind.FUNC)
    raises = set(func.extra["doc"]["raises"])
    assert raises == {"CustomError('wrapped')", "ValueError('bad flag')", "RuntimeError"}
    assert stats["funcs_with_raises"] == 1


def test_python_callsite_records_resolve_and_log():
    source = """
def helper():
    return 1


def alpha():
    value = helper()
    return value
"""
    indexer = PyFileIndexer("calls.py", source, enrich=True, call_cap=50)
    nodes, edges, callsites, stats = indexer.index()

    alpha_node = _node_by_symbol(nodes, "alpha", NodeKind.FUNC)
    helper_node = _node_by_symbol(nodes, "helper", NodeKind.FUNC)
    call_edges = [e for e in edges if e["type"] == "call"]
    assert len(call_edges) == 1
    assert call_edges[0]["dst"] == helper_node.node_id

    assert len(callsites) == 1
    call = callsites[0]
    assert call["caller_id"] == alpha_node.node_id
    assert call["callee_ref"]["type"] == "node_id"
    assert call["callee_ref"]["value"] != "helper"
    assert "helper(" in call["snippet"]
    assert stats["callsites_total"] == 1


@pytest.mark.skipif(os.getenv("CI") == "true", reason="tree-sitter setup required")
def test_ts_param_and_flags_enrichment():
    source = """
async function fetchData(url: string, retries = 2): Promise<void> {
  await doFetch(url);
}

const format = (value: string, ...rest: number[]): string => {
  return value + rest.length;
};

class Service {
  run(task?: string): void {
    this.helper();
  }

  helper() {}
}
"""
    indexer = TSFileIndexer("mod.ts", source, enrich=True, call_cap=50)
    nodes, edges, callsites, stats = indexer.index()

    fetch_fn = _node_by_symbol(nodes, "fetchData", NodeKind.FUNC)
    params = fetch_fn.extra["doc"]["params"]
    assert params[0]["name"] == "url"
    assert params[0]["annotation"] == "string"
    async_flag = fetch_fn.extra["doc"]["flags"]["async"]
    assert async_flag is True

    format_fn = _node_by_symbol(nodes, "format", NodeKind.FUNC)
    rest_param = format_fn.extra["doc"]["params"][-1]
    assert rest_param["kind"] == "rest"

    run_method = _node_by_symbol(nodes, "run", NodeKind.BLOCK)
    run_meta = run_method.extra["doc"]
    assert run_meta["is_method"] is True
    assert run_meta["owner"] == "Service"
    assert stats["funcs_total"] >= 3


def test_integration_build_with_enrichment(monkeypatch):
    with TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (repo / "pkg").mkdir()
        (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "pkg" / "mod.py").write_text(
            """
def helper(x: int) -> int:
    return x * 2


def public_api(val: int) -> int:
    return helper(val)
""",
            encoding="utf-8",
        )
        (repo / "pkg" / "mod.ts").write_text(
            """
export function format(input: string): string {
  return input.trim();
}

export function caller(msg: string) {
  return format(msg);
}
""",
            encoding="utf-8",
        )

        monkeypatch.setenv("CODEINDEX_FEATURE_DOCS_NODES_ENHANCED", "1")
        out_dir = Path(tmp) / "out"
        build(str(repo), str(out_dir), summarizer="off", summary_scope="none")

        nodes_path = out_dir / "nodes.jsonl"
        node_rows = list(load_jsonl(str(nodes_path)))
        func_row = next(row for row in node_rows if row["symbol"] == "public_api")
        assert func_row["extra"]["doc"]["params"]

        xref_path = out_dir / "xref_calls.jsonl"
        xrefs = [json.loads(line) for line in xref_path.read_text(encoding="utf-8").splitlines() if line]
        assert any(entry["caller_id"] == func_row["node_id"] for entry in xrefs)

        formatted = render_node_doc(_node_by_symbol(
            [node_from_dict(r) for r in node_rows], "public_api", NodeKind.FUNC
        ))
        assert "Args:" in formatted


def node_from_dict(data: dict) -> Node:
    return Node(
        node_id=data["node_id"],
        parent_id=data.get("parent_id"),
        kind=NodeKind(data["kind"]),
        path=data["path"],
        lang=data.get("lang", "python"),
        symbol=data.get("symbol"),
        signature=data.get("signature"),
        start_line=data.get("start_line"),
        end_line=data.get("end_line"),
        loc=data.get("loc"),
        summary=data.get("summary"),
        hash=data.get("hash"),
        extra=data.get("extra", {}),
    )
