from __future__ import annotations

import re
from pathlib import Path

from collections import defaultdict
from .builder import ArchBuilder


def _sanitize_identifier(label: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_]", "_", label)
    if not safe:
        safe = "pkg"
    if safe[0].isdigit():
        safe = f"pkg_{safe}"
    return safe


def write_package_mermaid(
    builder: ArchBuilder, output_path: Path, max_depth: int = 6
) -> None:
    deps = builder.package_dependencies(max_depth)
    nodes = sorted(deps.keys())

    node_ids = {pkg: _sanitize_identifier(pkg) for pkg in nodes}

    lines = ["graph LR"]
    declared_nodes: set[str] = set()
    for pkg in nodes:
        node_id = node_ids[pkg]
        lines.append(f'    {node_id}["{pkg}"]')
        declared_nodes.add(node_id)

    emitted_edges: set[tuple[str, str]] = set()
    for src, targets in sorted(deps.items()):
        src_id = node_ids[src]
        for dst in sorted(targets):
            dst_id = node_ids.setdefault(dst, _sanitize_identifier(dst))
            if dst_id not in declared_nodes:
                lines.append(f'    {dst_id}["{dst}"]')
                declared_nodes.add(dst_id)
            edge = (src_id, dst_id)
            if edge in emitted_edges:
                continue
            emitted_edges.add(edge)
            lines.append(f"    {src_id} --> {dst_id}")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_component_mermaid(
    component_deps: dict[str, set[str]], output_path: Path
) -> None:
    lines = ["graph LR"]
    declared: set[str] = set()
    for comp in sorted(component_deps.keys()):
        node_id = _sanitize_identifier(comp)
        lines.append(f'    {node_id}["{comp}"]')
        declared.add(node_id)
    for targets in component_deps.values():
        for comp in targets:
            node_id = _sanitize_identifier(comp)
            if node_id not in declared:
                lines.append(f'    {node_id}["{comp}"]')
                declared.add(node_id)

    emitted: set[tuple[str, str]] = set()
    for src, targets in sorted(component_deps.items()):
        src_id = _sanitize_identifier(src)
        for dst in sorted(targets):
            dst_id = _sanitize_identifier(dst)
            edge = (src_id, dst_id)
            if edge in emitted:
                continue
            emitted.add(edge)
            lines.append(f"    {src_id} --> {dst_id}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_connectivity_mermaid(
    builder: ArchBuilder, component_deps: dict[str, set[str]], output_path: Path
) -> None:
    """
    Produce a higher-level diagram that clusters components by inferred role tags.
    """
    role_clusters = defaultdict(set)
    for comp, packages in builder.components.items():
        tags = builder.component_role_tags(comp)
        if not tags:
            role_clusters["other"].add(comp)
        else:
            for tag in tags:
                role_clusters[tag].add(comp)

    lines = ["graph TD"]
    declared: set[str] = set()
    for role, comps in sorted(role_clusters.items()):
        cluster_name = _sanitize_identifier(role)
        lines.append(f"    subgraph {cluster_name}[{role.title()}]")
        for comp in sorted(comps):
            node_id = _sanitize_identifier(comp)
            lines.append(f'        {node_id}["{comp}"]')
            declared.add(node_id)
        lines.append("    end")

    for comp in builder.component_names():
        node_id = _sanitize_identifier(comp)
        if node_id not in declared:
            lines.append(f'    {node_id}["{comp}"]')
            declared.add(node_id)

    emitted: set[tuple[str, str]] = set()
    for src, targets in sorted(component_deps.items()):
        src_id = _sanitize_identifier(src)
        for dst in sorted(targets):
            dst_id = _sanitize_identifier(dst)
            edge = (src_id, dst_id)
            if edge in emitted:
                continue
            emitted.add(edge)
            lines.append(f"    {src_id} --> {dst_id}")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
