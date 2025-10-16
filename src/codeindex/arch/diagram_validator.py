from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Iterable, Mapping


_GRAPH_HEADER_RE = re.compile(r"^\s*graph\s+(?:LR|TD|RL|BT)\b", re.IGNORECASE)
_NODE_ID_RE = re.compile(r"^\s*([A-Za-z][\w\-]*)\s*(.*)$")
_EDGE_PATTERN = re.compile(
    r"(?P<src>[A-Za-z][\w\-]*)\s*[-.]{1,3}(?:\|[^|]+\|)?\s*>\s*(?P<dst>[A-Za-z][\w\-]*)"
)
_SUBGRAPH_PATTERN = re.compile(r"^\s*subgraph\b", re.IGNORECASE)
_COMMENT_PATTERN = re.compile(r"^\s*%%")


def _strip_label(text: str) -> str:
    label = text.strip()
    if label.startswith(("'", '"')) and label.endswith(("'", '"')) and len(label) >= 2:
        label = label[1:-1]
    return label.strip()


def _consume_delimited(text: str, opener: str, closer: str) -> tuple[str | None, str]:
    if not text.startswith(opener):
        return None, text
    start = len(opener)
    end = text.find(closer, start)
    if end == -1:
        return None, text
    label = text[start:end]
    remainder = text[end + len(closer) :]
    if remainder.startswith(":::"):
        remainder = ""
    return label, remainder


def _extract_node_definition(line: str) -> tuple[str, str] | None:
    match = _NODE_ID_RE.match(line)
    if not match:
        return None
    node_id, rest = match.group(1), match.group(2).lstrip()
    if not rest:
        return None
    label: str | None = None
    if rest.startswith("["):
        label, _ = _consume_delimited(rest, "[", "]")
    elif rest.startswith("(("):
        label, _ = _consume_delimited(rest, "((", "))")
    elif rest.startswith("("):
        label, _ = _consume_delimited(rest, "(", ")")
    elif rest.startswith("{"):
        label, _ = _consume_delimited(rest, "{", "}")
    elif rest.startswith("<"):
        label, _ = _consume_delimited(rest, "<", ">")
    if label is None:
        return None
    return node_id, _strip_label(label)


@dataclass
class DiagramParse:
    name: str
    raw: str
    nodes_by_id: dict[str, str] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def labels(self) -> set[str]:
        return set(self.nodes_by_id.values())


@dataclass
class DiagramValidationResult:
    name: str
    path: Path | None
    nodes: set[str]
    edges: list[tuple[str, str]]
    invalid_nodes: set[str]
    invalid_edges: list[tuple[str, str]]
    duplicate_edges: list[tuple[str, str]]
    errors: list[str]

    @property
    def is_valid(self) -> bool:
        return not (self.errors or self.invalid_nodes or self.invalid_edges)


@dataclass
class DiagramValidationSummary:
    results: list[DiagramValidationResult]
    coverage_ratio: float
    missing_nodes: set[str]
    invalid_refs: set[str]
    errors: list[str]

    @property
    def is_valid(self) -> bool:
        return (
            not self.errors
            and self.coverage_ratio >= 0
            and all(result.is_valid for result in self.results)
        )


class DiagramValidator:
    """
    Parses Mermaid flowcharts and ensures that all referenced nodes belong to the
    repository component/package inventory.
    """

    def __init__(self, allowed_nodes: Iterable[str], required_coverage: float = 0.95):
        allowed = set(allowed_nodes)
        if not allowed:
            raise ValueError("DiagramValidator requires at least one allowed node.")
        self.allowed_nodes = allowed
        self.required_coverage = required_coverage

    def parse(self, name: str, mermaid: str) -> DiagramParse:
        nodes_by_id: dict[str, str] = {}
        edges: list[tuple[str, str]] = []
        errors: list[str] = []

        lines = mermaid.splitlines()
        if not lines:
            errors.append("empty diagram")
            return DiagramParse(
                name=name, raw=mermaid, nodes_by_id={}, edges=[], errors=errors
            )

        if not _GRAPH_HEADER_RE.match(lines[0]):
            errors.append(
                "missing Mermaid graph header (expected `graph <dir>` on first line)"
            )

        for line_no, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or _COMMENT_PATTERN.match(stripped):
                continue
            if _SUBGRAPH_PATTERN.match(stripped):
                # clusters are labels, not component nodes; skip parsing.
                continue
            node_def = _extract_node_definition(stripped)
            if node_def:
                node_id, label = node_def
                if not label:
                    errors.append(f"line {line_no}: node `{node_id}` missing label")
                    label = node_id
                nodes_by_id[node_id] = label
                continue

            for edge_match in _EDGE_PATTERN.finditer(stripped):
                edges.append((edge_match.group("src"), edge_match.group("dst")))

        return DiagramParse(
            name=name, raw=mermaid, nodes_by_id=nodes_by_id, edges=edges, errors=errors
        )

    def validate_single(
        self, name: str, mermaid: str, *, path: Path | None = None
    ) -> DiagramValidationResult:
        parse = self.parse(name, mermaid)
        nodes: set[str] = set(parse.nodes_by_id.values())
        invalid_nodes = {node for node in nodes if node not in self.allowed_nodes}
        invalid_edges: list[tuple[str, str]] = []
        duplicate_edges: list[tuple[str, str]] = []
        seen_edges: set[tuple[str, str]] = set()

        for src_id, dst_id in parse.edges:
            src_label = parse.nodes_by_id.get(src_id, src_id)
            dst_label = parse.nodes_by_id.get(dst_id, dst_id)
            edge = (src_label, dst_label)
            if edge in seen_edges:
                duplicate_edges.append(edge)
            else:
                seen_edges.add(edge)
            if (
                src_label not in self.allowed_nodes
                or dst_label not in self.allowed_nodes
            ):
                invalid_edges.append(edge)

        readable_edges = [
            (parse.nodes_by_id.get(src, src), parse.nodes_by_id.get(dst, dst))
            for src, dst in parse.edges
        ]
        return DiagramValidationResult(
            name=name,
            path=path,
            nodes=nodes,
            edges=readable_edges,
            invalid_nodes=invalid_nodes,
            invalid_edges=invalid_edges,
            duplicate_edges=duplicate_edges,
            errors=parse.errors,
        )

    def validate(
        self,
        diagrams: Mapping[str, str],
        *,
        paths: Mapping[str, Path] | None = None,
    ) -> DiagramValidationSummary:
        results: list[DiagramValidationResult] = []
        covered_nodes: set[str] = set()
        invalid_refs: set[str] = set()
        summary_errors: list[str] = []

        for name, mermaid in diagrams.items():
            path = paths[name] if paths and name in paths else None
            result = self.validate_single(name, mermaid, path=path)
            covered_nodes.update(
                node for node in result.nodes if node in self.allowed_nodes
            )
            invalid_refs.update(result.invalid_nodes)
            for src, dst in result.invalid_edges:
                if src not in self.allowed_nodes:
                    invalid_refs.add(src)
                if dst not in self.allowed_nodes:
                    invalid_refs.add(dst)
            results.append(result)

        missing_nodes = self.allowed_nodes - covered_nodes
        coverage_ratio = 1.0
        if self.allowed_nodes:
            coverage_ratio = 1.0 - (len(missing_nodes) / len(self.allowed_nodes))

        if coverage_ratio < self.required_coverage:
            summary_errors.append(
                f"coverage {coverage_ratio:.3f} fell below required {self.required_coverage:.2f}"
            )
        if invalid_refs:
            summary_errors.append(
                f"diagrams referenced unknown components: {sorted(invalid_refs)}"
            )

        return DiagramValidationSummary(
            results=results,
            coverage_ratio=coverage_ratio,
            missing_nodes=missing_nodes,
            invalid_refs=invalid_refs,
            errors=summary_errors,
        )
