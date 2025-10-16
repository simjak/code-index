from __future__ import annotations

from typing import Iterable

from .nodes import FunctionParam, Node, NodeKind


def render_node_doc(node: Node) -> str:
    if node.kind in (NodeKind.FUNC, NodeKind.BLOCK):
        return _render_function_doc(node)
    if node.kind == NodeKind.CLASS:
        return _render_class_doc(node)
    if node.kind == NodeKind.FILE:
        meta = (node.extra or {}).get("doc", {})
        docstring = meta.get("docstring") if isinstance(meta, dict) else None
        return (docstring or node.summary or "").strip()
    return node.summary or ""


def _render_function_doc(node: Node) -> str:
    meta = (node.extra or {}).get("doc", {})
    lines: list[str] = []

    docstring = meta.get("docstring") if isinstance(meta, dict) else None
    if docstring:
        lines.extend(_split_and_strip(docstring))
        lines.append("")

    params = _coerce_params(meta.get("params")) if isinstance(meta, dict) else []
    if params:
        lines.append("Args:")
        for param in params:
            descr = _format_param(param)
            lines.append(f"    {descr}")
        lines.append("")

    returns = meta.get("returns") if isinstance(meta, dict) else None
    if returns is not None:
        lines.append("Returns:")
        lines.append(f"    {returns or 'None'}")
        lines.append("")

    raises = meta.get("raises") if isinstance(meta, dict) else []
    if raises:
        lines.append("Raises:")
        for exc in raises:
            lines.append(f"    {exc}")
        lines.append("")

    rendered = "\n".join(line.rstrip() for line in lines).strip()
    if rendered:
        return rendered
    if node.summary:
        return node.summary.strip()
    return ""  # no metadata available


def _render_class_doc(node: Node) -> str:
    meta = (node.extra or {}).get("doc", {})
    docstring = meta.get("docstring") if isinstance(meta, dict) else None
    if docstring:
        return "\n".join(_split_and_strip(docstring)).strip()
    return node.summary or ""


def _coerce_params(params: object) -> list[FunctionParam]:
    if not isinstance(params, Iterable):
        return []
    result: list[FunctionParam] = []
    for item in params:
        if isinstance(item, dict) and "name" in item:
            result.append(item)  # type: ignore[arg-type]
    return result


def _format_param(param: FunctionParam) -> str:
    name = param.get("name", "param")
    annotation = param.get("annotation")
    default = param.get("default")
    pieces = [name]
    if annotation:
        pieces.append(f"({annotation})")
    if default is not None:
        pieces.append(f"= {default}")
    return " ".join(pieces)


def _split_and_strip(text: str) -> list[str]:
    return [line.rstrip() for line in text.strip().splitlines() if line.strip()]


__all__ = ["render_node_doc"]
