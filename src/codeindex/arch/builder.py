from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
import re
from ..store import load_jsonl


def _ensure_path(obj: os.PathLike[str] | str) -> Path:
    return obj if isinstance(obj, Path) else Path(obj)


@dataclass(slots=True)
class ArchNode:
    node_id: str
    parent_id: str | None
    kind: str
    path: str
    symbol: str | None = None
    summary: str | None = None
    loc: int | None = None
    extra: dict = field(default_factory=dict)


class ArchBuilder:
    """
    Light-weight loader around the indexed nodes/edges that provides convenience
    queries needed by the architecture module.
    """

    def __init__(self, nodes: dict[str, ArchNode], edges: list[dict]):
        self.nodes_by_id = nodes
        self.edges = edges
        self.children = self._build_children_index()
        self.packages = {nid: node for nid, node in nodes.items() if node.kind == "pkg"}
        self.files = {nid: node for nid, node in nodes.items() if node.kind == "file"}
        self.file_to_package = self._map_file_to_package()
        self.pkg_by_symbol = self._map_package_symbols()
        self.component_by_pkg, self.components = self._map_components()

    @classmethod
    def from_index(cls, index_dir: os.PathLike[str] | str) -> "ArchBuilder":
        index_path = _ensure_path(index_dir)
        nodes = {}
        nodes_file = index_path / "nodes.jsonl"
        if not nodes_file.exists():
            raise FileNotFoundError(f"Missing nodes.jsonl in {index_path}")
        for raw in load_jsonl(str(nodes_file)):
            node = ArchNode(
                node_id=raw["node_id"],
                parent_id=raw.get("parent_id"),
                kind=raw["kind"],
                path=raw.get("path") or "",
                symbol=raw.get("symbol"),
                summary=raw.get("summary"),
                loc=raw.get("loc"),
                extra=raw.get("extra") or {},
            )
            nodes[node.node_id] = node

        edges_file = index_path / "edges.jsonl"
        edges: list[dict] = []
        if edges_file.exists():
            edges = list(load_jsonl(str(edges_file)))

        return cls(nodes, edges)

    @staticmethod
    def json_dumps(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)

    def _build_children_index(self) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {}
        for node in self.nodes_by_id.values():
            if node.parent_id is None:
                continue
            children.setdefault(node.parent_id, []).append(node.node_id)
        return children

    def _map_file_to_package(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for file_id, node in self.files.items():
            parent = node.parent_id
            while parent:
                parent_node = self.nodes_by_id.get(parent)
                if parent_node is None:
                    break
                if parent_node.kind == "pkg":
                    mapping[file_id] = parent_node.node_id
                    break
                parent = parent_node.parent_id
        return mapping

    def _map_package_symbols(self) -> dict[str, set[str]]:
        """Map package symbol and path aliases to node IDs."""
        aliases: dict[str, set[str]] = {}
        for node_id, node in self.packages.items():
            parts = [node.symbol or "", os.path.basename(node.path)]
            dotted = node.path.replace("/", ".")
            parts.append(dotted)
            for alias in {p for p in parts if p}:
                aliases.setdefault(alias, set()).add(node_id)
        return aliases

    def _map_components(self) -> tuple[dict[str, str], dict[str, set[str]]]:
        comp_by_pkg: dict[str, str] = {}
        components: dict[str, set[str]] = {}
        for pkg_id, node in self.packages.items():
            comp = self._component_name(node.path)
            comp_by_pkg[pkg_id] = comp
            components.setdefault(comp, set()).add(pkg_id)
        return comp_by_pkg, components

    def descendants(self, node_id: str) -> list[str]:
        """Return node IDs for the subtree rooted at the provided node id."""
        result: list[str] = []
        stack = [node_id]
        while stack:
            current = stack.pop()
            for child in self.children.get(current, []):
                result.append(child)
                stack.append(child)
        return result

    def package_path(self, pkg_id: str) -> str:
        node = self.packages[pkg_id]
        return node.path

    def package_dependencies(self, max_depth: int = 6) -> dict[str, set[str]]:
        """
        Build a simple package-to-package import graph. The heuristic prefers
        internal packages and ignores self-loops.
        """
        deps: dict[str, set[str]] = {pkg.path: set() for pkg in self.packages.values()}
        for edge in self.edges:
            if edge.get("type") != "import":
                continue
            src = edge.get("src")
            dst = edge.get("dst")
            if src not in self.file_to_package:
                continue
            src_pkg_id = self.file_to_package[src]
            src_pkg_path = self.package_path(src_pkg_id)
            target_pkg_id = self._resolve_import_target(src_pkg_id, dst)
            if target_pkg_id is None:
                continue
            if target_pkg_id == src_pkg_id:
                continue
            deps[src_pkg_path].add(self.package_path(target_pkg_id))
        # trim depth notionally by collapsing beyond max_depth (currently no-op)
        return deps

    def component_dependencies(self) -> dict[str, set[str]]:
        """Aggregate import edges between top-level components."""
        comp_deps: dict[str, set[str]] = {comp: set() for comp in self.components}
        known_components = set(self.components)
        for edge in self.edges:
            if edge.get("type") != "import":
                continue
            src = edge.get("src")
            if src not in self.file_to_package:
                continue
            src_pkg = self.file_to_package[src]
            src_comp = self.component_by_pkg.get(src_pkg)
            if not src_comp:
                continue
            dst_pkg = self._resolve_import_target(src_pkg, edge.get("dst"))
            dst_comp: str | None = None
            if dst_pkg:
                dst_comp = self.component_by_pkg.get(dst_pkg)
            else:
                dst_comp = self._infer_component_from_import(
                    edge.get("dst"), known_components
                )
            if dst_comp and dst_comp != src_comp:
                comp_deps.setdefault(src_comp, set()).add(dst_comp)
        return comp_deps

    def _resolve_import_target(self, source_pkg_id: str, dst: str | None) -> str | None:
        """
        Attempt to resolve the destination of an import edge to a package node id.

        This relies on heuristics because Python/TS imports are recorded as plain
        strings in the index.
        """
        if not dst:
            return None
        if dst in self.nodes_by_id:
            node = self.nodes_by_id[dst]
            if node.kind == "pkg":
                return node.node_id
            if node.kind == "file":
                return self.file_to_package.get(node.node_id)
            parent = node.parent_id
            while parent:
                parent_node = self.nodes_by_id.get(parent)
                if parent_node is None:
                    break
                if parent_node.kind == "pkg":
                    return parent_node.node_id
                parent = parent_node.parent_id
            return None

        # Heuristic based mapping
        cleaned = dst.strip("'\"")
        components = [part for part in cleaned.replace("/", ".").split(".") if part]

        # 1) direct alias match
        for comp in components:
            ids = self.pkg_by_symbol.get(comp)
            if ids:
                if len(ids) == 1:
                    return next(iter(ids))
                # prefer package that matches source ancestry
                for pid in ids:
                    if self._is_same_branch(source_pkg_id, pid):
                        return pid
                return next(iter(ids))

        # 2) relative import (e.g., 'logger.logger'); assume same package
        return source_pkg_id if components else None

    def _infer_component_from_import(
        self, dst: str | None, known_components: set[str]
    ) -> str | None:
        if not dst:
            return None
        cleaned = dst.strip("'\"")
        tokens = re.split(r"[./]", cleaned)
        tokens = [
            tok
            for tok in tokens
            if tok and tok not in {"src", "lib", "apps", "packages"}
        ]
        for token in tokens:
            if token in known_components:
                return token
        return None

    @staticmethod
    def _component_name(path: str) -> str:
        if not path:
            return "root"
        parts = path.split("/")
        if parts[0] in {"src", "apps", "packages"} and len(parts) > 1:
            return parts[1]
        return parts[0]

    def component_for_package(self, pkg_id: str) -> str:
        return self.component_by_pkg[pkg_id]

    def component_for_node(self, node: ArchNode) -> str | None:
        if not node.path:
            return None
        name = self._component_name(node.path)
        return name

    def component_names(self) -> list[str]:
        return sorted(self.components.keys())

    def iter_component_nodes(self, component: str) -> list[ArchNode]:
        prefix = component + "/"
        result: list[ArchNode] = []
        for node in self.nodes_by_id.values():
            path = node.path or ""
            if not path:
                continue
            if path == component or path.startswith(prefix):
                result.append(node)
        return result

    def component_files(self, component: str) -> list[ArchNode]:
        prefix = component + "/"
        files: list[ArchNode] = []
        for node in self.files.values():
            path = node.path or ""
            if path == component or path.startswith(prefix):
                files.append(node)
        return files

    def component_role_tags(self, component: str) -> set[str]:
        tags: set[str] = set()
        nodes = self.iter_component_nodes(component)
        for node in nodes:
            path = (node.path or "").lower()
            if not path:
                continue
            if any(tok in path for tok in ("/routes/", "router", "api/")):
                tags.add("api")
            if path.endswith(".tsx") or path.endswith(".jsx") or "components/" in path:
                tags.add("frontend")
            if "worker" in path or "workflow" in path or "temporal" in path:
                tags.add("worker")
            if "lambda" in path:
                tags.add("lambda")
            if "test" in path or "spec" in path or "fixtures" in path:
                tags.add("tests")
            if "infra" in path or "terraform" in path:
                tags.add("infrastructure")
        name = component.lower()
        if "app" in name or "ui" in name or "web" in name:
            tags.add("frontend")
        if any(tok in name for tok in ("api", "backend", "server")):
            tags.add("api")
        if "worker" in name or "temporal" in name:
            tags.add("worker")
        if "lambda" in name:
            tags.add("lambda")
        if "test" in name or "e2e" in name:
            tags.add("tests")
        if "infra" in name:
            tags.add("infrastructure")
        return tags

    def _is_same_branch(self, src_pkg: str, candidate_pkg: str) -> bool:
        """Check whether candidate_pkg is in the ancestry chain of src_pkg."""
        if src_pkg == candidate_pkg:
            return True
        parent = self.packages[src_pkg].parent_id
        while parent:
            if parent == candidate_pkg:
                return True
            parent_node = self.nodes_by_id.get(parent)
            if parent_node is None:
                break
            parent = parent_node.parent_id
        return False

    @cached_property
    def repo_root(self) -> ArchNode:
        for node in self.nodes_by_id.values():
            if node.kind == "repo":
                return node
        raise RuntimeError("Index missing repo node")

    def packages_under(self, node_id: str | None = None) -> list[str]:
        """
        Return package node IDs beneath the provided node (defaults to repo root).
        """
        if node_id is None:
            node_id = self.repo_root.node_id
        result: list[str] = []
        stack = [node_id]
        while stack:
            current = stack.pop()
            for child in self.children.get(current, []):
                node = self.nodes_by_id[child]
                if node.kind == "pkg":
                    result.append(child)
                    stack.append(child)
        return result
