from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logger import logger
from .builder import ArchBuilder
from .diagram_validator import DiagramValidationSummary, DiagramValidator
from .llm_engine import LLMClient, LLMConfig
from .structure import select_important_files


@dataclass
class LLMArchitectConfig:
    out_dir: Path
    model: str = "gpt-5-mini"
    max_tokens: int = 4000
    temperature: float | None = None
    reasoning_effort: str = "medium"
    verbosity: str | None = "medium"
    stub: bool = False
    diagram_mode: str = "llm"
    coverage_threshold: float = 0.95
    max_redrafts: int = 2


class LLMArchitect:
    """
    Generates architecture documentation exclusively via an LLM. Deterministic
    heuristics are only used to prepare context, not to emit final docs.
    """

    def __init__(self, builder: ArchBuilder, config: LLMArchitectConfig):
        self.builder = builder
        self.config = config
        self.component_names = [
            name for name in self.builder.component_names() if name != "root"
        ]
        allowed_nodes = self.component_names or ["root"]
        if not self.component_names:
            self.component_names = allowed_nodes
        self.validator = DiagramValidator(
            allowed_nodes=allowed_nodes,
            required_coverage=config.coverage_threshold,
        )
        self.uses_llm = config.diagram_mode != "deterministic"
        self.client: Optional[LLMClient] = None
        if self.uses_llm:
            self.client = LLMClient(
                LLMConfig(
                    model=config.model,
                    max_completion_tokens=config.max_tokens,
                    temperature=config.temperature,
                    reasoning_effort=config.reasoning_effort,
                    verbosity=config.verbosity,
                    stub_enabled=config.stub,
                )
            )
        self.last_summary: Optional[DiagramValidationSummary] = None

    async def generate(self) -> None:
        docs_dir = self.config.out_dir / "docs"
        diagrams_dir = self.config.out_dir / "diagrams"
        docs_dir.mkdir(parents=True, exist_ok=True)
        diagrams_dir.mkdir(parents=True, exist_ok=True)

        contexts: Dict[str, Dict[str, Any]] = {}
        component_deps_raw = self.builder.component_dependencies()
        component_deps = {
            component: sorted(list(deps))
            for component, deps in component_deps_raw.items()
        }

        if self.config.diagram_mode == "deterministic" or not self.uses_llm:
            summary = self._generate_deterministic(
                docs_dir, diagrams_dir, component_deps, contexts=contexts
            )
            self.last_summary = summary
            return

        (docs_dir / "components").mkdir(exist_ok=True)
        component_diagrams: Dict[str, str] = {}
        diagram_paths: Dict[str, Path] = {}

        for component in self.component_names:
            context = self._build_component_context(component, component_deps)
            contexts[component] = context
            doc_text, diagram_text = await self._render_component(component, context)
            doc_path = docs_dir / "components" / f"{component}.md"
            doc_path.write_text(doc_text, encoding="utf-8")
            diag_path = diagrams_dir / f"{component}.mmd"
            diag_path.write_text(diagram_text, encoding="utf-8")
            component_diagrams[component] = diagram_text
            diagram_paths[component] = diag_path

        system_diagram = await self._render_system_diagram(contexts)
        system_path = diagrams_dir / "system.mmd"
        system_path.write_text(system_diagram, encoding="utf-8")
        component_diagrams["system"] = system_diagram
        diagram_paths["system"] = system_path

        summary = self.validator.validate(component_diagrams, paths=diagram_paths)
        if not self._summary_passes(summary):
            summary = await self._redraft_diagrams(
                contexts,
                component_diagrams,
                diagram_paths,
                docs_dir,
                diagrams_dir,
                summary,
            )
        if not self._summary_passes(summary):
            if self.config.diagram_mode == "hybrid":
                logger.warning(
                    "LLM diagrams failed validation after %s attempts; falling back to deterministic mode",
                    self.config.max_redrafts,
                )
                summary = self._generate_deterministic(
                    docs_dir, diagrams_dir, component_deps, contexts=contexts
                )
                self.last_summary = summary
                return
            raise RuntimeError(
                "Failed to produce valid component diagrams after redraft attempts: "
                f"coverage={summary.coverage_ratio:.3f}, invalid_refs={sorted(summary.invalid_refs)}"
            )

        self.last_summary = summary
        if not self.client:
            raise RuntimeError("LLM client unavailable for overview generation")
        overview = await self.client.overview_doc(list(contexts.values()))
        (docs_dir / "index.md").write_text(overview, encoding="utf-8")

    def _build_component_context(
        self, component: str, component_deps: Dict[str, List[str]]
    ) -> Dict[str, Any]:
        packages = [
            self.builder.packages[pkg_id].path
            for pkg_id in self.builder.components.get(component, [])
        ]
        nodes = self.builder.iter_component_nodes(component)
        stats = {
            "files": sum(1 for n in nodes if n.kind == "file"),
            "classes": sum(1 for n in nodes if n.kind == "class"),
            "functions": sum(1 for n in nodes if n.kind in ("func", "block")),
        }
        dependencies = component_deps.get(component, [])
        dependents = sorted(
            comp
            for comp, deps in component_deps.items()
            if component in deps and comp != component
        )
        important_files = [
            f.path
            for f in select_important_files(self.builder.component_files(component))
        ]

        context: Dict[str, Any] = {
            "name": component,
            "packages": packages[:20],
            "stats": stats,
            "dependencies": dependencies,
            "dependents": dependents,
            "roles": sorted(self.builder.component_role_tags(component)),
            "key_files": important_files[:10],
        }
        return context

    async def _render_component(
        self,
        component: str,
        context: Dict[str, Any],
        *,
        feedback: str | None = None,
    ) -> tuple[str, str]:
        if not self.client:
            raise RuntimeError("LLM client not initialized for component rendering")
        result = await self.client.component_doc(
            context,
            allowed_nodes=self.validator.allowed_nodes,
            feedback=feedback,
        )
        doc_text = result["documentation_md"].strip() + "\n"
        diagram_text = result["diagram_mermaid"].strip() + "\n"
        return doc_text, diagram_text

    async def _render_system_diagram(
        self,
        contexts: Dict[str, Dict[str, Any]],
        *,
        feedback: str | None = None,
    ) -> str:
        if not self.client:
            raise RuntimeError("LLM client not initialized for system diagram")
        diagram = await self.client.overview_diagram(
            list(contexts.values()),
            allowed_nodes=self.validator.allowed_nodes,
            feedback=feedback,
        )
        return diagram.strip() + "\n"

    def _summary_passes(self, summary: DiagramValidationSummary) -> bool:
        if summary.coverage_ratio < self.validator.required_coverage:
            return False
        if summary.invalid_refs:
            return False
        for result in summary.results:
            if result.errors or result.invalid_nodes or result.invalid_edges:
                return False
        return True

    async def _redraft_diagrams(
        self,
        contexts: Dict[str, Dict[str, Any]],
        component_diagrams: Dict[str, str],
        diagram_paths: Dict[str, Path],
        docs_dir: Path,
        diagrams_dir: Path,
        summary: DiagramValidationSummary,
    ) -> DiagramValidationSummary:
        attempt = 1
        updated_summary = summary
        while attempt < self.config.max_redrafts and not self._summary_passes(
            updated_summary
        ):
            attempt += 1
            feedback_map = self._build_feedback(updated_summary)
            if not feedback_map:
                break
            for component, feedback in feedback_map.items():
                if component == "system":
                    system_diagram = await self._render_system_diagram(
                        contexts, feedback=feedback
                    )
                    diag_path = diagram_paths.get("system")
                    if diag_path is not None:
                        diag_path.write_text(system_diagram, encoding="utf-8")
                    component_diagrams["system"] = system_diagram
                    continue
                context = contexts.get(component)
                if context is None:
                    continue
                doc_text, diagram_text = await self._render_component(
                    component,
                    context,
                    feedback=feedback,
                )
                doc_path = docs_dir / "components" / f"{component}.md"
                diag_path = diagram_paths.get(
                    component, diagrams_dir / f"{component}.mmd"
                )
                doc_path.write_text(doc_text, encoding="utf-8")
                diag_path.write_text(diagram_text, encoding="utf-8")
                component_diagrams[component] = diagram_text
                diagram_paths[component] = diag_path

            updated_summary = self.validator.validate(
                component_diagrams, paths=diagram_paths
            )
        return updated_summary

    def _build_feedback(self, summary: DiagramValidationSummary) -> Dict[str, str]:
        feedback: Dict[str, str] = {}
        allowed = ", ".join(sorted(self.validator.allowed_nodes))

        for result in summary.results:
            reasons: List[str] = []
            if result.errors:
                reasons.extend(result.errors)
            if result.invalid_nodes:
                invalid_list = ", ".join(sorted(result.invalid_nodes))
                reasons.append(
                    f"Diagram referenced unknown nodes: {invalid_list}. "
                    "Limit nodes to the provided component list."
                )
            if result.invalid_edges:
                edge_descriptions = ", ".join(
                    f"{src}->{dst}" for src, dst in result.invalid_edges
                )
                reasons.append(
                    f"Edges point to unknown components ({edge_descriptions}). "
                    "Remove or relabel them to valid components."
                )
            if reasons:
                reasons.append(f"Valid node labels: {allowed}")
                feedback[result.name] = "\n".join(reasons)

        if (
            summary.coverage_ratio < self.validator.required_coverage
            and summary.missing_nodes
        ):
            missing = ", ".join(sorted(summary.missing_nodes))
            # Apply coverage feedback to all diagrams without explicit issues.
            coverage_message = (
                f"Increase coverage to at least {self.validator.required_coverage:.2f}. "
                f"Include the following missing components in at least one node label: {missing}."
            )
            for component in [*self.component_names, "system"]:
                feedback.setdefault(component, coverage_message)

        return feedback

    def _generate_deterministic(
        self,
        docs_dir: Path,
        diagrams_dir: Path,
        component_deps: Dict[str, List[str]],
        *,
        contexts: Dict[str, Dict[str, Any]] | None = None,
    ) -> DiagramValidationSummary:
        contexts = contexts or {}
        (docs_dir / "components").mkdir(exist_ok=True)
        component_diagrams: Dict[str, str] = {}
        diagram_paths: Dict[str, Path] = {}

        for component in self.component_names:
            context = contexts.get(component)
            if context is None:
                context = self._build_component_context(component, component_deps)
                contexts[component] = context
            doc_text = self._deterministic_component_doc(context)
            diag_text = self._deterministic_component_diagram(component, context)
            doc_path = docs_dir / "components" / f"{component}.md"
            diag_path = diagrams_dir / f"{component}.mmd"
            doc_path.write_text(doc_text, encoding="utf-8")
            diag_path.write_text(diag_text, encoding="utf-8")
            component_diagrams[component] = diag_text
            diagram_paths[component] = diag_path

        system_diagram = self._deterministic_system_diagram(component_deps)
        system_path = diagrams_dir / "system.mmd"
        system_path.write_text(system_diagram, encoding="utf-8")
        component_diagrams["system"] = system_diagram
        diagram_paths["system"] = system_path

        summary = self.validator.validate(component_diagrams, paths=diagram_paths)
        if not self._summary_passes(summary):
            raise RuntimeError(
                "Deterministic diagrams failed validation: "
                f"coverage={summary.coverage_ratio:.3f}, invalid_refs={sorted(summary.invalid_refs)}"
            )

        overview = self._deterministic_overview_doc(contexts)
        (docs_dir / "index.md").write_text(overview, encoding="utf-8")
        return summary

    def _deterministic_component_doc(self, context: Dict[str, Any]) -> str:
        name = context["name"]
        roles = ", ".join(context.get("roles", [])) or "unknown"
        stats = context.get("stats", {})
        dependencies = ", ".join(context.get("dependencies", [])) or "none"
        dependents = ", ".join(context.get("dependents", [])) or "none"
        key_files = context.get("key_files", [])[:5]
        files_section = (
            "\n".join(f"- `{path}`" for path in key_files)
            if key_files
            else "- _(no key files)_"
        )
        lines = [
            f"# Component: {name}",
            "",
            f"**Roles**: {roles}",
            "",
            "## Responsibilities",
            f"- Files tracked: {stats.get('files', 0)}",
            f"- Depends on: {dependencies}",
            f"- Used by: {dependents}",
            "",
            "## Key Files",
            files_section,
            "",
        ]
        return "\n".join(lines)

    def _deterministic_component_diagram(
        self, component: str, context: Dict[str, Any]
    ) -> str:
        allowed = set(self.validator.allowed_nodes)
        lines = ["graph TD", f'    {component}["{component}"]']
        declared = {component}
        emitted = set()
        for dep in context.get("dependencies", []):
            if dep not in allowed or dep == component:
                continue
            if dep not in declared:
                lines.append(f'    {dep}["{dep}"]')
                declared.add(dep)
            edge = (component, dep)
            if edge in emitted:
                continue
            emitted.add(edge)
            lines.append(f"    {component} --> {dep}")
        return "\n".join(lines) + "\n"

    def _deterministic_system_diagram(
        self, component_deps: Dict[str, List[str]]
    ) -> str:
        lines = ["graph TD"]
        for comp in self.component_names:
            lines.append(f'    {comp}["{comp}"]')
        emitted: set[tuple[str, str]] = set()
        allowed = self.validator.allowed_nodes
        for src, targets in sorted(component_deps.items()):
            if src not in allowed:
                continue
            for dst in targets:
                if dst not in allowed or dst == src:
                    continue
                edge = (src, dst)
                if edge in emitted:
                    continue
                emitted.add(edge)
                lines.append(f"    {src} --> {dst}")
        return "\n".join(lines) + "\n"

    def _deterministic_overview_doc(self, contexts: Dict[str, Dict[str, Any]]) -> str:
        lines = ["# Repository Overview", ""]
        lines.append("## Components")
        lines.append("")
        for component in sorted(contexts.keys()):
            context = contexts[component]
            stats = context.get("stats", {})
            roles = ", ".join(context.get("roles", [])) or "unknown"
            dependencies = ", ".join(context.get("dependencies", [])) or "none"
            lines.append(f"### {component}")
            lines.append(f"- Roles: {roles}")
            lines.append(
                f"- Size: {stats.get('files', 0)} files · {stats.get('functions', 0)} functions"
            )
            lines.append(f"- Depends on: {dependencies}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
