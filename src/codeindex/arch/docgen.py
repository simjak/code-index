from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .builder import ArchBuilder, ArchNode
from .flows import FlowSummary
from .structure import select_important_files


def generate_docs(
    builder: ArchBuilder,
    flows: list[FlowSummary],
    component_deps: dict[str, set[str]],
    out_dir: Path,
) -> dict:
    records = _build_component_records(builder, flows)
    docs_dir = out_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    _write_architecture_md(docs_dir / "architecture.md", records, flows, component_deps)
    _write_components_md(docs_dir / "components.md", records, flows, component_deps)
    _write_connectivity_md(
        docs_dir / "how_everything_connects.md",
        builder,
        records,
        component_deps,
        flows,
    )
    _write_flow_guides(
        docs_dir / "flows.md",
        builder,
        flows,
        component_deps,
        records,
    )
    return records


def _build_component_records(
    builder: ArchBuilder, flows: Iterable[FlowSummary]
) -> dict[str, dict]:
    flow_map = defaultdict(list)
    for flow in flows:
        flow_map[flow.component].append(flow)

    records: dict[str, dict] = {}
    for component in builder.component_names():
        if component == "root":
            continue
        nodes = builder.iter_component_nodes(component)
        files = builder.component_files(component)
        stats = _collect_stats(nodes)
        role_tags = _infer_component_roles(component, nodes)
        important = select_important_files(files)[:5]
        packages = sorted(builder.components.get(component, []))
        flows_for_component = flow_map.get(component, [])
        records[component] = {
            "component": component,
            "stats": stats,
            "role_tags": role_tags,
            "important": [imp.to_dict() for imp in important],
            "packages": [builder.packages[pid].path for pid in packages],
            "flows": [flow.__dict__ for flow in flows_for_component],
        }
    return records


def _collect_stats(nodes: Iterable[ArchNode]) -> dict[str, int]:
    stats = {"files": 0, "classes": 0, "functions": 0, "loc": 0}
    for node in nodes:
        if node.kind == "file":
            stats["files"] += 1
            stats["loc"] += node.loc or 0
        elif node.kind == "class":
            stats["classes"] += 1
        elif node.kind in ("func", "block"):
            stats["functions"] += 1
    return stats


def _infer_component_roles(component: str, nodes: Iterable[ArchNode]) -> list[str]:
    tags: set[str] = set()
    low_name = component.lower()
    if any(token in low_name for token in ("app", "web", "ui", "vendor")):
        tags.add("frontend")
    if any(token in low_name for token in ("backend", "api", "server")):
        tags.add("backend")
    if "worker" in low_name or "temporal" in low_name:
        tags.add("worker")
    if "lambda" in low_name:
        tags.add("lambda")
    if "infra" in low_name or "terraform" in low_name:
        tags.add("infrastructure")
    if "test" in low_name or "e2e" in low_name or "integration" in low_name:
        tags.add("tests")

    for node in nodes:
        path = (node.path or "").lower()
        if not path:
            continue
        if "routes" in path or "router" in path or "api/" in path:
            tags.add("api")
        if path.endswith(".tsx") or path.endswith(".jsx") or "components/" in path:
            tags.add("frontend")
        if "/worker" in path or "/workflow" in path:
            tags.add("worker")
        if "infra" in path or "terraform" in path:
            tags.add("infrastructure")
        if "tests" in path or "spec" in path:
            tags.add("tests")
    return sorted(tags)


def _write_architecture_md(
    path: Path,
    records: dict[str, dict],
    flows: list[FlowSummary],
    component_deps: dict[str, set[str]],
) -> None:
    total_components = len(records)
    total_flows = len(flows)
    lines: list[str] = []
    lines.append("# Architecture Overview")
    lines.append("")
    lines.append(f"- Components indexed: **{total_components}**")
    lines.append(f"- Key flows extracted: **{total_flows}**")
    lines.append("")
    lines.append("## Component Inventory")
    lines.append("")
    lines.append("| Component | Roles | Files | Functions | Key Files |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, record in sorted(records.items()):
        stats = record["stats"]
        roles = ", ".join(record["role_tags"]) if record["role_tags"] else "—"
        important = record["important"][:3]
        key_files = ", ".join(item["name"] for item in important) if important else "—"
        lines.append(
            f"| `{name}` | {roles} | {stats['files']} | {stats['functions']} | {key_files} |"
        )
    lines.append("")

    lines.append("## Component Dependencies")
    lines.append("")
    lines.append("```mermaid")
    lines.append("graph LR")
    for src, targets in sorted(component_deps.items()):
        if not targets:
            continue
        for dst in sorted(targets):
            lines.append(f"    {src} --> {dst}")
    lines.append("```")
    lines.append("")

    lines.append("## Key Flows")
    lines.append("")
    for name, record in sorted(records.items()):
        flows_for_component = record["flows"]
        if not flows_for_component:
            continue
        lines.append(f"### `{name}`")
        lines.append("")
        for flow in flows_for_component[:8]:
            entry = flow["entry_symbol"] or flow["entry_path"]
            category = flow["category"]
            calls = flow["calls"]
            call_str = ", ".join(calls[:6]) if calls else "—"
            summary = flow["summary"] or ""
            if len(summary) > 160:
                summary = summary[:157].rstrip() + "..."
            lines.append(f"- **{category}** · `{entry}` → {call_str}")
            if summary:
                lines.append(f"  - {summary}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_components_md(
    path: Path,
    records: dict[str, dict],
    flows: list[FlowSummary],
    component_deps: dict[str, set[str]],
) -> None:
    lines: list[str] = ["# Component Reference", ""]
    for name, record in sorted(records.items()):
        lines.append(f"## `{name}`")
        roles = ", ".join(record["role_tags"]) if record["role_tags"] else "—"
        stats = record["stats"]
        lines.append(f"- **Roles**: {roles}")
        lines.append(
            f"- **Stats**: files={stats['files']}, functions={stats['functions']}, classes={stats['classes']}"
        )
        packages = record["packages"]
        if packages:
            lines.append(f"- **Packages**: {', '.join(packages[:6])}")
        important = record["important"]
        if important:
            lines.append("- **Key Files**:")
            for item in important[:5]:
                summary = item.get("summary") or ""
                if len(summary) > 120:
                    summary = summary[:117].rstrip() + "..."
                lines.append(f"  - `{item['path']}` — {summary}")
        flows_for_component = [flow for flow in flows if flow.component == name]
        if flows_for_component:
            lines.append("- **Flows**:")
            for flow in flows_for_component[:8]:
                calls = ", ".join(flow.calls[:6]) if flow.calls else "—"
                entry = flow.entry_symbol or flow.entry_path
                lines.append(f"  - [{flow.category}] `{entry}` → {calls}")
        deps = component_deps.get(name)
        if deps:
            lines.append(f"- **Depends on**: {', '.join(sorted(deps))}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_connectivity_md(
    path: Path,
    builder: ArchBuilder,
    records: dict[str, dict],
    component_deps: dict[str, set[str]],
    flows: list[FlowSummary],
) -> None:
    lines: list[str] = ["# How Everything Connects", ""]
    lines.append(
        "This guide summarizes how major components collaborate across the monorepo,"
        " combining dependency analysis with extracted flows."
    )
    lines.append("")
    lines.append("## Quick Start")
    lines.append("")
    lines.append(
        "1. Open `../diagrams/how_everything_connects.mmd` in a Mermaid viewer."
    )
    lines.append("2. Trace arrows to see which component calls into another.")
    lines.append("3. Use the flow sections below to jump into code entrypoints.")
    lines.append("")

    lines.append("## Component Highlights")
    lines.append("")
    for name, record in sorted(records.items(), key=lambda item: item[0]):
        deps = component_deps.get(name, set())
        dependents = {
            other
            for other, targets in component_deps.items()
            if name in targets and other != name
        }
        lines.append(f"### `{name}`")
        lines.append(
            f"- **Roles**: {', '.join(record['role_tags']) if record['role_tags'] else '—'}"
        )
        lines.append(f"- **Depends on**: {', '.join(sorted(deps)) if deps else '—'}")
        lines.append(
            f"- **Used by**: {', '.join(sorted(dependents)) if dependents else '—'}"
        )
        flow_refs = [
            flow
            for flow in flows
            if flow.component == name and flow.calls and len(flow.calls) > 0
        ]
        if flow_refs:
            preview = ", ".join(
                f"`{flow.entry_symbol or flow.entry_path}`" for flow in flow_refs[:3]
            )
            lines.append(f"- **Key flows**: {preview}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_flow_guides(
    path: Path,
    builder: ArchBuilder,
    flows: list[FlowSummary],
    component_deps: dict[str, set[str]],
    records: dict[str, dict],
) -> None:
    lines: list[str] = ["# Flow Guides", ""]
    lines.append(
        "This document curates end-to-end flows extracted from the repository."
        " Each section links the main entrypoints, downstream dependencies, and"
        " supporting services."
    )
    lines.append("")

    guides = [
        (
            "Vendor Onboarding",
            [
                "vendor",
                "workloads",
                "workloads-python",
                "temporal-python-worker",
                "public-api",
                "backoffice",
                "app",
            ],
            [
                "vendor",
                "workloads",
                "workloads-python",
                "temporal-python-worker",
                "public-api",
            ],
            ["vendor", "workloads", "temporal"],
        ),
        (
            "Evidence Processing",
            [
                "app",
                "workloads",
                "temporal-python-worker",
                "docling-lambda",
                "temporal-worker",
            ],
            [
                "workloads",
                "temporal-python-worker",
                "docling-lambda",
                "temporal-worker",
            ],
            ["evidence", "docling", "lambda"],
        ),
    ]

    for title, core_components, focus_components, keywords in guides:
        lines.append(f"## {title}")
        lines.append("")
        involved = [comp for comp in core_components if comp in records]
        if involved:
            lines.append(
                "- **Primary components**: "
                + ", ".join(f"`{comp}`" for comp in involved)
            )
        related = set()
        for comp in focus_components:
            related.update(component_deps.get(comp, set()))
        related = [comp for comp in sorted(related) if comp not in involved]
        if related:
            lines.append(
                "- **Secondary dependencies**: "
                + (", ".join(f"`{comp}`" for comp in related) if related else "—")
            )

        relevant_flows = [
            flow
            for flow in flows
            if (
                flow.component in focus_components
                or any(kw in (flow.entry_path or "").lower() for kw in keywords)
                or any(kw in (flow.entry_symbol or "").lower() for kw in keywords)
            )
        ]
        relevant_flows.sort(
            key=lambda f: (
                0 if f.component in focus_components else 1,
                -(f.loc or 0),
                f.entry_path or "",
            )
        )

        if not relevant_flows:
            lines.append("- No flows detected yet. Run `codeindex build` to refresh.")
            lines.append("")
            continue

        lines.append("- **Key entrypoints**:")
        for flow in relevant_flows[:15]:
            entry = flow.entry_symbol or flow.entry_path
            calls = ", ".join(flow.calls[:5]) if flow.calls else "—"
            summary = flow.summary or ""
            if len(summary) > 140:
                summary = summary[:137].rstrip() + "..."
            bullet = (
                f"  - `{flow.component}` → `{entry}`"
                f" · category={flow.category}; calls: {calls}"
            )
            lines.append(bullet)
            if summary:
                lines.append(f"    - {summary}")

        lines.append("")

        key_files: list[str] = []
        for comp in focus_components:
            record = records.get(comp)
            if not record:
                continue
            for item in record["important"][:3]:
                key_files.append(item["path"])
        if key_files:
            lines.append("- **Key files to inspect**:")
            for path_str in key_files[:8]:
                lines.append(f"  - `{path_str}`")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
