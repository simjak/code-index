from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .builder import ArchBuilder, ArchNode


@dataclass
class ImportantFile:
    path: str
    name: str
    loc: int | None
    summary: str | None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "loc": self.loc,
            "summary": self.summary,
        }


@dataclass
class DirectorySummary:
    path: str
    symbol: str | None
    role_tags: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    important: list[ImportantFile] = field(default_factory=list)
    children: list["DirectorySummary"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "role_tags": self.role_tags,
            "stats": self.stats,
            "important": [item.to_dict() for item in self.important],
            "children": [child.to_dict() for child in self.children],
        }


PREFERRED_FILENAMES = [
    "cli.py",
    "indexer.py",
    "searcher.py",
    "bm25.py",
    "llm_search.py",
]


def build_directory_summary(builder: ArchBuilder) -> DirectorySummary:
    repo = builder.repo_root
    repo_name = repo.symbol or Path(repo.path).name or "repository"
    root = DirectorySummary(
        path=repo_name,
        symbol=repo.symbol,
        role_tags=[],
        stats=_aggregate_stats(builder, repo.node_id),
        important=[],
    )
    pkg_ids = sorted(
        builder.packages_under(repo.node_id),
        key=lambda pid: builder.package_path(pid),
    )
    root.children = [_build_package_summary(builder, pid) for pid in pkg_ids]
    return root


def _build_package_summary(builder: ArchBuilder, pkg_id: str) -> DirectorySummary:
    pkg = builder.packages[pkg_id]
    files = _files_in_package(builder, pkg_id)
    tags = _infer_role_tags(pkg, files)
    stats = _aggregate_stats(builder, pkg_id)
    important = select_important_files(files)
    child_pkg_ids = [
        cid
        for cid in builder.children.get(pkg_id, [])
        if builder.nodes_by_id[cid].kind == "pkg"
    ]
    children = [_build_package_summary(builder, cid) for cid in child_pkg_ids]
    return DirectorySummary(
        path=pkg.path,
        symbol=pkg.symbol,
        role_tags=sorted(tags),
        stats=stats,
        important=important,
        children=children,
    )


def _aggregate_stats(builder: ArchBuilder, root_id: str) -> dict[str, int]:
    counters = {"files": 0, "classes": 0, "functions": 0}
    if builder.nodes_by_id[root_id].kind == "file":
        counters["files"] = 1
    for nid in builder.descendants(root_id):
        node = builder.nodes_by_id[nid]
        if node.kind == "file":
            counters["files"] += 1
        elif node.kind == "class":
            counters["classes"] += 1
        elif node.kind in ("func", "block"):
            counters["functions"] += 1
    return counters


def _files_in_package(builder: ArchBuilder, pkg_id: str) -> list[ArchNode]:
    files = [
        builder.files[file_id]
        for file_id, owner in builder.file_to_package.items()
        if owner == pkg_id
    ]
    files.sort(key=lambda node: node.path)
    return files


def _infer_role_tags(pkg: ArchNode, files: Iterable[ArchNode]) -> set[str]:
    tags: set[str] = set()
    file_names = {os.path.basename(f.path) for f in files}
    if pkg.path.startswith("tests") or any(
        name.startswith("test") for name in file_names
    ):
        tags.add("tests")
    if "cli.py" in file_names:
        tags.add("cli")
    if any("indexer" in name for name in file_names):
        tags.add("indexer")
    if any(
        name in {"searcher.py", "bm25.py", "llm_search.py"}
        or name.endswith("_search.py")
        for name in file_names
    ):
        tags.add("search")
    if any(
        name.endswith("_routes.py") or name.endswith("_api.py") for name in file_names
    ):
        tags.add("web")
    return tags


def select_important_files(files: list[ArchNode]) -> list[ImportantFile]:
    if not files:
        return []
    priority = {name: idx for idx, name in enumerate(PREFERRED_FILENAMES)}

    def sort_key(node: ArchNode):
        name = os.path.basename(node.path)
        pref = priority.get(name, len(priority) + 1)
        loc = node.loc or 0
        return (pref, -loc, name)

    sorted_files = sorted(files, key=sort_key)
    top = sorted_files[:5]
    results: list[ImportantFile] = []
    for node in top:
        name = os.path.basename(node.path)
        results.append(
            ImportantFile(
                path=node.path,
                name=name,
                loc=node.loc,
                summary=node.summary,
            )
        )
    return results


def _format_stats(stats: dict[str, int]) -> str:
    parts = []
    for key in ("files", "classes", "functions"):
        value = stats.get(key)
        if value:
            parts.append(f"{key}: {value}")
    return ", ".join(parts)


def _markdown_lines(node: DirectorySummary, depth: int = 0) -> list[str]:
    indent = "  " * depth
    bullet = "- " if depth > 0 else ""
    tags = f" ({', '.join(node.role_tags)})" if node.role_tags else ""
    lines = [f"{indent}{bullet}{node.path}{tags}"]

    stats_text = _format_stats(node.stats)
    if stats_text:
        lines.append(f"{indent}  - stats: {stats_text}")
    if node.important:
        lines.append(f"{indent}  - important files:")
        for item in node.important:
            summary = item.summary or ""
            if len(summary) > 90:
                summary = summary[:87].rstrip() + "..."
            lines.append(
                f"{indent}    - {item.name}" + (f" — {summary}" if summary else "")
            )
    for child in node.children:
        lines.extend(_markdown_lines(child, depth + 1))
    return lines


def _mindmap_lines(node: DirectorySummary) -> list[str]:
    lines = ["mindmap"]

    def walk(current: DirectorySummary, depth: int):
        prefix = "  " * depth + "* "
        label = current.path
        if current.role_tags:
            label += f" ({', '.join(current.role_tags)})"
        lines.append(f"{prefix}{label}")
        for child in current.children:
            walk(child, depth + 1)

    walk(node, 0)
    return lines


def write_structure_outputs(
    out_dir: Path, summary: DirectorySummary, formats: set[str]
) -> None:
    struct_dir = out_dir / "structure"
    struct_dir.mkdir(parents=True, exist_ok=True)
    if "json" in formats:
        (struct_dir / "summary.json").write_text(
            ArchBuilder.json_dumps(summary.to_dict()), encoding="utf-8"
        )
    if "md" in formats:
        md_lines = ["# Directory Structure"]
        for child in summary.children:
            md_lines.extend(_markdown_lines(child, depth=0))
            md_lines.append("")
        (struct_dir / "structure.md").write_text(
            "\n".join(line.rstrip() for line in md_lines).rstrip() + "\n",
            encoding="utf-8",
        )
    if "mermaid" in formats:
        (struct_dir / "structure.mmd").write_text(
            "\n".join(_mindmap_lines(summary)), encoding="utf-8"
        )
