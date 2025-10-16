from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .builder import ArchBuilder, ArchNode


@dataclass
class FlowSummary:
    component: str
    category: str
    entry_path: str
    entry_symbol: str | None
    loc: int | None
    summary: str | None
    calls: list[str]


ENTRY_KEYWORDS = {
    "main",
    "handler",
    "run",
    "start",
    "workflow",
    "worker",
    "execute",
    "process",
    "task",
}


def extract_flows(
    builder: ArchBuilder, limit_per_component: int = 12
) -> list[FlowSummary]:
    """Heuristically identify important entry points and their immediate call fan-out."""
    flows_by_component: Dict[str, List[FlowSummary]] = {}
    seen: set[str] = set()

    def record(node: ArchNode, category: str):
        if node.node_id in seen:
            return
        seen.add(node.node_id)
        component = builder.component_for_node(node) or "root"
        calls = _collect_calls(builder, node.node_id)
        summary = FlowSummary(
            component=component,
            category=category,
            entry_path=node.path,
            entry_symbol=node.symbol,
            loc=node.loc,
            summary=node.summary,
            calls=calls[:15],
        )
        flows_by_component.setdefault(component, []).append(summary)

    # Primary pass: obvious entry scripts
    for node in builder.nodes_by_id.values():
        if node.kind == "file":
            low_path = (node.path or "").lower()
            if any(keyword in low_path for keyword in ("cli", "main.py", "manage.py")):
                record(node, "cli")
            if any(token in low_path for token in ("workflow", "worker")):
                record(node, "workflow")
            if "/routes/" in low_path or low_path.endswith("routes.py"):
                record(node, "route-module")

    # Functions that look like entry handlers
    for node in builder.nodes_by_id.values():
        if node.kind not in ("func", "block"):
            continue
        parent = builder.nodes_by_id.get(node.parent_id) if node.parent_id else None
        if parent and parent.kind not in ("file", "class"):
            continue
        if not parent or parent.kind == "file":
            if _looks_like_entry(node):
                record(node, _classify_category(node.path or "", node.symbol or ""))
        if (
            parent
            and parent.kind == "file"
            and "/routes/" in (parent.path or "").lower()
        ):
            record(node, "http-route")

    # Trim per component
    final: list[FlowSummary] = []
    for component, summaries in flows_by_component.items():
        summaries.sort(
            key=lambda s: (
                _category_rank(s.category),
                -(s.loc or 0),
                s.entry_path or "",
            )
        )
        final.extend(summaries[:limit_per_component])
    return final


def _classify_category(path: str, symbol: str) -> str:
    low_path = path.lower()
    low_sym = symbol.lower()
    if "temporal" in low_path or "workflow" in low_path or "worker" in low_path:
        return "workflow"
    if "routes" in low_path or low_sym in {"get", "post", "put", "delete"}:
        return "http-route"
    if "cli" in low_path or low_sym in {"main", "cli", "entrypoint"}:
        return "cli"
    if "task" in low_path or "job" in low_path or low_sym in {"task", "job"}:
        return "task"
    return "function"


def _category_rank(category: str) -> int:
    order = {
        "cli": 0,
        "workflow": 1,
        "http-route": 2,
        "task": 3,
        "route-module": 4,
        "function": 5,
    }
    return order.get(category, 9)


def _looks_like_entry(node: ArchNode) -> bool:
    sym = (node.symbol or "").lower()
    path = (node.path or "").lower()
    if sym in ENTRY_KEYWORDS:
        return True
    if any(token in path for token in ("workflow", "worker", "routes", "cli")):
        return True
    if sym.startswith("handle_") or sym.endswith("_handler"):
        return True
    return False


def _collect_calls(builder: ArchBuilder, node_id: str) -> list[str]:
    calls: list[str] = []
    for edge in builder.edges:
        if edge.get("type") != "call":
            continue
        if edge.get("src") != node_id:
            continue
        dst = edge.get("dst")
        detail = edge.get("detail")
        label: str | None = None
        if dst and dst in builder.nodes_by_id:
            target = builder.nodes_by_id[dst]
            label = target.symbol or target.path or dst
        else:
            label = detail or dst
        if label:
            calls.append(label)
    return calls
