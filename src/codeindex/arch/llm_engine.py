from __future__ import annotations

import json
import os
from dataclasses import dataclass
import re
from typing import Any, Dict, List, Sequence

from ..logger import logger


@dataclass
class LLMConfig:
    model: str = "gpt-5-mini"
    max_completion_tokens: int = 4000
    temperature: float | None = 0.2
    reasoning_effort: str = "medium"
    verbosity: str | None = "medium"
    stub_enabled: bool = False


class LLMClient:
    """
    Thin wrapper around OpenAI chat completions with a deterministic stub mode
    for tests/CI. When CODEINDEX_LLM_STUB=1, responses are synthesized locally.
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self.stub = config.stub_enabled or os.getenv("CODEINDEX_LLM_STUB") == "1"
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.stub and not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY must be set or CODEINDEX_LLM_STUB=1 for LLM generation"
            )
        self._client = None

    def _ensure_client(self):
        if self.stub:
            return
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "openai package is required for LLM generation. "
                    "Install with `uv add openai`."
                ) from exc
            self._client = AsyncOpenAI(api_key=self.api_key)

    async def component_doc(
        self,
        context: Dict[str, Any],
        *,
        allowed_nodes: Sequence[str] | None = None,
        feedback: str | None = None,
    ) -> Dict[str, str]:
        if self.stub:
            return self._stub_component_doc(context, allowed_nodes=allowed_nodes)

        payload = {
            "context": context,
            "allowed_nodes": list(allowed_nodes) if allowed_nodes else None,
            "feedback": feedback,
        }
        prompt = json.dumps(payload, ensure_ascii=False)
        messages: List[Dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are an expert software architect. Think through the context, then output only what is asked. "
                    "Follow these rules: use Markdown only where semantically correct; avoid HTML; never invent modules "
                    "that are not present in the provided data; keep tone factual and concise."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Respond with a strict JSON object with keys "
                    "`documentation_md` and `diagram_mermaid`. "
                    "`documentation_md` must be Markdown summarizing the component purpose, key modules, "
                    "external dependencies, and operational considerations. "
                    "`diagram_mermaid` must be a valid Mermaid graph depicting the component's relationships.\n"
                    "Rules:\n"
                    "- Only use node labels from the allowed list when provided.\n"
                    "- Ensure every referenced node is declared exactly once.\n"
                    "- If feedback is provided, address it explicitly in the redraft.\n"
                    f"Component request:\n{prompt}"
                ),
            },
        ]
        response = await self._complete(messages)
        response = _sanitize_json_like(response)
        try:
            data = json.loads(response)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM response was not valid JSON: {response}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("LLM response must be a JSON object.")
        return {
            "documentation_md": data.get("documentation_md", "").strip(),
            "diagram_mermaid": data.get("diagram_mermaid", "").strip(),
        }

    async def overview_diagram(
        self,
        components: Sequence[Dict[str, Any]],
        *,
        allowed_nodes: Sequence[str],
        feedback: str | None = None,
    ) -> str:
        if self.stub:
            return self._stub_overview_diagram(components, allowed_nodes=allowed_nodes)

        payload = {
            "components": components,
            "allowed_nodes": list(allowed_nodes),
            "feedback": feedback,
        }
        prompt = json.dumps(payload, ensure_ascii=False)
        allowed_csv = ", ".join(allowed_nodes)
        instructions = (
            "You are an expert software architect preparing a repository-level component interaction diagram. "
            "Produce a Mermaid flowchart (graph TD or graph LR) with nodes whose labels exactly match the allowed component names. "
            "Do not add code fences or commentary—return the Mermaid graph only.\n"
            f"Allowed node labels: {allowed_csv}\n"
            "If feedback is provided, adjust the output accordingly.\n"
            f"Context:\n{prompt}"
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert architect. Think carefully before responding. "
                    "Follow instructions precisely and output Mermaid content only."
                ),
            },
            {
                "role": "user",
                "content": instructions,
            },
        ]
        response = await self._complete(messages)
        return response.strip()

    async def overview_doc(self, components: List[Dict[str, Any]]) -> str:
        if self.stub:
            lines = ["# Repository Overview", ""]
            for comp in components:
                lines.append(f"- **{comp['name']}**: {comp['stats']['files']} files")
            return "\n".join(lines) + "\n"
        prompt = json.dumps({"components": components}, ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert staff engineer documenting a software repository. Think first, then respond. "
                    "Produce a Markdown overview that summarizes boundaries, collaboration patterns, and risks. "
                    "Respect semantic Markdown, avoid tables unless needed, and ground every statement in the supplied data."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Respond with Markdown only (no JSON, no code fences). "
                    "Structure the response with headings and bullet lists where useful.\n"
                    f"Component inventory:\n{prompt}"
                ),
            },
        ]
        raw = await self._complete(messages)
        raw = _sanitize_json_like(raw) if raw.strip().startswith("{") else raw
        return raw.strip() + "\n"

    async def _complete(self, messages: List[Dict[str, str]]) -> str:
        self._ensure_client()
        logger.debug(
            "LLM request: model=%s tokens=%d",
            self.config.model,
            self.config.max_completion_tokens,
        )
        is_reasoning_model = "gpt-5" in self.config.model.lower()
        params: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_completion_tokens": self.config.max_completion_tokens,
        }
        if is_reasoning_model:
            params["reasoning_effort"] = self.config.reasoning_effort
            if self.config.verbosity:
                params["verbosity"] = self.config.verbosity
        elif self.config.temperature is not None:
            params["temperature"] = self.config.temperature

        completion = await self._client.chat.completions.create(  # type: ignore[union-attr]
            **params
        )
        return completion.choices[0].message.content or ""

    def _stub_component_doc(
        self, context: Dict[str, Any], *, allowed_nodes: Sequence[str] | None = None
    ) -> Dict[str, str]:
        name = context.get("name", "component")
        deps = ", ".join(context.get("dependencies", [])) or "none"
        dependents = ", ".join(context.get("dependents", [])) or "none"
        key_files = context.get("key_files", [])[:3]
        files_list = (
            "\n".join(f"- `{path}`" for path in key_files) or "- _(no key files)_"
        )
        doc = (
            f"# Component: {name}\n\n"
            f"**Roles**: {', '.join(context.get('roles', [])) or 'unknown'}\n\n"
            "## Responsibilities\n"
            f"- Files tracked: {context.get('stats', {}).get('files', 0)}\n"
            f"- Depends on: {deps}\n"
            f"- Used by: {dependents}\n\n"
            "## Key Files\n"
            f"{files_list}\n"
        )
        diagram_lines = ["graph TD", f'    {name}["{name}"]']
        declared: set[str] = {name}
        allowed = set(allowed_nodes or [])
        for dep in context.get("dependencies", []):
            if allowed and dep not in allowed:
                continue
            if dep not in declared:
                diagram_lines.append(f'    {dep}["{dep}"]')
                declared.add(dep)
            diagram_lines.append(f"    {name} --> {dep}")
        diagram = "\n".join(diagram_lines) + "\n"
        return {"documentation_md": doc, "diagram_mermaid": diagram}

    def _stub_overview_diagram(
        self,
        components: Sequence[Dict[str, Any]],
        *,
        allowed_nodes: Sequence[str],
    ) -> str:
        allowed = list(dict.fromkeys(allowed_nodes))
        lines = ["graph TD"]
        declared: set[str] = set()
        for name in allowed:
            lines.append(f'    {name}["{name}"]')
            declared.add(name)

        emitted: set[tuple[str, str]] = set()
        for component in components:
            src = component.get("name")
            if src not in declared or src not in allowed:
                continue
            for dep in component.get("dependencies", []):
                if dep not in allowed or dep == src:
                    continue
                edge = (src, dep)
                if edge in emitted:
                    continue
                emitted.add(edge)
                lines.append(f"    {src} --> {dep}")
        return "\n".join(lines) + "\n"


_BAD_ESCAPE_RE = re.compile(r'\\([^"\\/bfnrtu])')


def _sanitize_json_like(text: str) -> str:
    cleaned = _BAD_ESCAPE_RE.sub(lambda m: "\\\\" + m.group(1), text)
    cleaned = cleaned.replace("\\\n", "\\\\\n")
    if cleaned.endswith("\\"):
        cleaned = cleaned[:-1] + "\\\\"
    return cleaned
