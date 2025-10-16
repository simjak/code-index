from __future__ import annotations

import ast
import hashlib
import os
import textwrap
from typing import Any

from .logger import logger
from .nodes import (
    CallsiteRecord,
    CallsiteRef,
    FunctionDocMetadata,
    FunctionParam,
    Node,
    NodeKind,
)


DEFAULT_CALLSITE_CAP = 200


def stable_id(
    kind: str,
    path: str,
    symbol: str | None,
    start: int | None,
    end: int | None,
) -> str:
    s = f"{kind}\t{path}\t{symbol or ''}\t{start or 0}\t{end or 0}"
    import hashlib

    return hashlib.sha1(s.encode()).hexdigest()


def first_line(s: str | None) -> str | None:
    if not s:
        return None
    return textwrap.dedent(s).strip().splitlines()[0][:400]


class PyFileIndexer(ast.NodeVisitor):
    def __init__(
        self,
        rel_path: str,
        file_text: str,
        *,
        enrich: bool = False,
        call_cap: int = DEFAULT_CALLSITE_CAP,
    ):
        self.rel_path = rel_path
        self.text = file_text
        self.nodes: list[Node] = []
        self.edges: list[dict[str, Any]] = []
        self.stack: list[str] = []
        self.defined_symbols: dict[str, str] = {}
        self.calls: dict[str, set[str]] = {}
        self.enrich = enrich
        self.call_cap = call_cap
        self.callsites: list[CallsiteRecord] = []
        self.stats: dict[str, int] = {
            "funcs_total": 0,
            "funcs_with_params": 0,
            "funcs_with_returns": 0,
            "funcs_with_raises": 0,
            "funcs_with_decorators": 0,
            "raises_extracted_total": 0,
            "callsites_total": 0,
            "callsite_cap_hits": 0,
        }
        self.file_node: Node | None = None
        self.class_stack: list[str] = []

    def parent_id(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def _register(
        self,
        kind: NodeKind,
        name: str | None,
        node: ast.AST,
        summary: str | None,
        signature: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Node:
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None) or start
        n = Node(
            node_id=stable_id(kind.value, self.rel_path, name, start, end),
            parent_id=self.parent_id(),
            kind=kind,
            path=self.rel_path,
            symbol=name,
            signature=signature,
            start_line=start,
            end_line=end,
            loc=(end - start + 1) if (start and end) else None,
            summary=first_line(summary),
        )
        if extra:
            n.extra = extra
        self.nodes.append(n)
        if name:
            self.defined_symbols[name] = n.node_id
        return n

    def index(
        self,
    ) -> tuple[list[Node], list[dict[str, Any]], list[CallsiteRecord], dict[str, int]]:
        tree = ast.parse(self.text)
        mod_doc = ast.get_docstring(tree)
        total_lines = len(self.text.splitlines())
        fnode_extra = None
        if self.enrich:
            fnode_extra = {
                "doc": {
                    "lang": "python",
                    "docstring": mod_doc,
                }
            }
        fnode = Node(
            node_id=stable_id("file", self.rel_path, None, 1, total_lines),
            parent_id=None,
            kind=NodeKind.FILE,
            path=self.rel_path,
            lang="python",
            symbol=os.path.basename(self.rel_path),
            start_line=1,
            end_line=total_lines,
            loc=total_lines,
            summary=first_line(mod_doc),
            hash=hashlib.sha1(self.text.encode()).hexdigest(),
            extra=fnode_extra or {},
        )
        self.nodes.append(fnode)
        self.file_node = fnode
        self.stack.append(fnode.node_id)
        super().generic_visit(tree)
        self.stack.pop()
        self._collect_import_edges(tree, fnode)
        for fn_id, names in self.calls.items():
            for nm in sorted(names):
                dst = self.defined_symbols.get(nm, nm)
                self.edges.append(
                    {"src": fn_id, "dst": dst, "type": "call", "detail": nm}
                )
        return self.nodes, self.edges, self.callsites, self.stats

    def visit_ClassDef(self, node: ast.ClassDef):
        doc = ast.get_docstring(node)
        extra: dict[str, Any] | None = None
        if self.enrich:
            bases = [self._expr_to_str(b) for b in node.bases]
            bases = [b for b in bases if b]
            decorators = [self._expr_to_str(d) for d in node.decorator_list]
            decorators = [d for d in decorators if d]
            extra = {
                "doc": {
                    "lang": "python",
                    "docstring": doc,
                    "bases": bases,
                    "decorators": decorators,
                    "visibility": self._visibility(node.name),
                }
            }
        cnode = self._register(NodeKind.CLASS, node.name, node, doc, extra=extra)
        for b in node.bases:
            bn = self._expr_to_str(b)
            if bn:
                dst = self.defined_symbols.get(bn, bn)
                self.edges.append(
                    {"src": cnode.node_id, "dst": dst, "type": "inherit", "detail": bn}
                )
        self.stack.append(cnode.node_id)
        self.class_stack.append(node.name)
        super().generic_visit(node)
        self.class_stack.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_function(node, is_async=True)

    def visit_Assign(self, node: ast.Assign):
        if len(self.stack) == 1:
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) >= 2:
                    self._register(NodeKind.CONST, t.id, node, summary=None)

    # Helpers -----------------------------------------------------------------

    def _visit_function(self, node: ast.AST, *, is_async: bool) -> None:
        name = getattr(node, "name", None)
        if not isinstance(name, str):  # defensive, should not happen
            return
        signature, params_meta = self._format_params(node)
        doc = ast.get_docstring(node)
        is_method = bool(self.class_stack)
        owner = self.class_stack[-1] if is_method else None
        kind = NodeKind.BLOCK if is_method else NodeKind.FUNC
        fn_extra: dict[str, Any] | None = None

        fnode = self._register(kind, name, node, doc, signature=signature)
        if owner:
            self.defined_symbols[f"{owner}.{name}"] = fnode.node_id

        self.stats["funcs_total"] += 1

        if self.enrich:
            returns = self._expr_to_str(getattr(node, "returns", None))
            raises = self._collect_raises(node)
            decorators = [
                self._expr_to_str(d) for d in getattr(node, "decorator_list", [])
            ]
            decorators = [d for d in decorators if d]
            is_generator = self._is_generator(node)
            meta: FunctionDocMetadata = {
                "lang": "python",
                "params": params_meta,
                "visibility": self._visibility(name),
                "is_async": is_async,
                "is_method": is_method,
            }
            if owner:
                meta["owner"] = owner
            if doc:
                meta["docstring"] = doc
            if returns is not None:
                meta["returns"] = returns
                self.stats["funcs_with_returns"] += 1
            if raises:
                meta["raises"] = raises
                self.stats["funcs_with_raises"] += 1
                self.stats["raises_extracted_total"] += len(raises)
            if decorators:
                meta["decorators"] = decorators
                self.stats["funcs_with_decorators"] += 1
            meta["flags"] = {"generator": is_generator}
            self.stats["funcs_with_params"] += 1
            fn_extra = {"doc": meta}
            fnode.extra = fn_extra

        self._collect_calls_for_function(node, fnode)

    def _collect_calls_for_function(self, node: ast.AST, fnode: Node) -> None:
        names: set[str] = set()
        call_records: list[CallsiteRecord] = []

        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            edge_symbol, display_symbol = self._symbol_from_call(call)
            if edge_symbol is None and display_symbol is None:
                continue
            symbol_key = edge_symbol or display_symbol
            if symbol_key:
                names.add(symbol_key)

            resolved_id = None
            if edge_symbol:
                resolved_id = self.defined_symbols.get(edge_symbol)
            if resolved_id is None and display_symbol:
                resolved_id = self.defined_symbols.get(display_symbol)

            line = getattr(call, "lineno", None)
            snippet = ast.get_source_segment(self.text, call)
            if snippet:
                snippet = snippet.strip()

            reason = "not_defined_in_file"
            if resolved_id is None and self.enrich:
                logger.debug(
                    "DEBUG: unresolved callee %s in %s:%s (%s)",
                    display_symbol or edge_symbol,
                    self.rel_path,
                    line,
                    reason,
                )

            if self.enrich:
                ref: CallsiteRef
                if resolved_id:
                    ref = {
                        "type": "node_id",
                        "value": resolved_id,
                    }
                    if edge_symbol or display_symbol:
                        ref["symbol"] = display_symbol or edge_symbol  # type: ignore[assignment]
                else:
                    ref = {
                        "type": "unresolved",
                        "value": display_symbol or edge_symbol or "<unknown>",
                        "symbol": display_symbol or edge_symbol,
                        "reason": reason,
                    }
                call_records.append(
                    {
                        "caller_id": fnode.node_id,
                        "callee_ref": ref,
                        "file": self.rel_path,
                        "line": line,
                        "snippet": snippet,
                    }
                )

        self.calls[fnode.node_id] = names

        if not self.enrich or not call_records:
            return

        capped = call_records[: self.call_cap]
        if len(call_records) > self.call_cap:
            self.stats["callsite_cap_hits"] += 1
            logger.debug(
                "DEBUG: callsite cap hit for %s (%d>%d)",
                fnode.node_id,
                len(call_records),
                self.call_cap,
            )
        self.callsites.extend(capped)
        self.stats["callsites_total"] += len(capped)

    def _symbol_from_call(self, call: ast.Call) -> tuple[str | None, str | None]:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id, func.id
        if isinstance(func, ast.Attribute):
            try:
                display = ast.unparse(func)
            except Exception:
                display = func.attr
            return func.attr, display
        return None, None

    def _collect_import_edges(self, tree: ast.AST, file_node: Node) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.edges.append(
                        {
                            "src": file_node.node_id,
                            "dst": alias.name,
                            "type": "import",
                            "detail": alias.asname or alias.name,
                        }
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    qual = f"{module}.{alias.name}" if module else alias.name
                    self.edges.append(
                        {
                            "src": file_node.node_id,
                            "dst": qual,
                            "type": "import",
                            "detail": alias.asname or alias.name,
                        }
                    )

    def _expr_to_str(self, node: ast.AST | None) -> str | None:
        if node is None:
            return None
        try:
            return ast.unparse(node)
        except Exception:
            return None

    def _visibility(self, name: str) -> str:
        if name.startswith("__") and not name.endswith("__"):
            return "private"
        if name.startswith("_"):
            return "protected"
        return "public"

    def _collect_raises(self, node: ast.AST) -> list[str]:
        if not self.enrich:
            return []
        seen: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Raise):
                expr = self._expr_to_str(child.exc)
                if expr:
                    seen.add(expr)
            elif isinstance(child, ast.ExceptHandler):
                expr = self._expr_to_str(child.type)
                if expr:
                    seen.add(expr)
        return sorted(seen)

    def _is_generator(self, node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, (ast.Yield, ast.YieldFrom)):
                return True
        return False

    def _format_params(self, node: ast.AST) -> tuple[str, list[FunctionParam]]:
        arguments: ast.arguments = getattr(node, "args")  # type: ignore[assignment]
        params_meta: list[FunctionParam] = []
        parts: list[str] = []

        pos = list(arguments.posonlyargs) + list(arguments.args)
        defaults = list(arguments.defaults)
        default_offset = len(pos) - len(defaults)

        def build_param(
            arg: ast.arg,
            *,
            kind: str,
            default_expr: ast.AST | None = None,
            prefix: str = "",
        ) -> tuple[str, FunctionParam]:
            annotation = self._expr_to_str(arg.annotation)
            default_str = self._expr_to_str(default_expr)
            meta: FunctionParam = {"name": arg.arg, "kind": kind}
            if annotation is not None:
                meta["annotation"] = annotation
            if default_str is not None:
                meta["default"] = default_str
            display = prefix + arg.arg
            if annotation is not None:
                display += f": {annotation}"
            if default_str is not None:
                display += f" = {default_str}"
            return display, meta

        # positional (pos-only + positional-or-keyword)
        for idx, arg in enumerate(pos):
            default_expr = None
            if idx >= default_offset:
                default_expr = defaults[idx - default_offset]
            kind = (
                "positional_only"
                if idx < len(arguments.posonlyargs)
                else "positional_or_keyword"
            )
            display, meta = build_param(arg, kind=kind, default_expr=default_expr)
            parts.append(display)
            params_meta.append(meta)
            if idx + 1 == len(arguments.posonlyargs) and arguments.posonlyargs:
                parts.append("/")

        # var positional
        if arguments.vararg:
            display, meta = build_param(arguments.vararg, kind="var_positional")
            parts.append("*" + display)
            params_meta.append(meta)

        # keyword-only marker if needed
        if arguments.kwonlyargs and not arguments.vararg:
            parts.append("*")

        # keyword-only args
        for idx, arg in enumerate(arguments.kwonlyargs):
            default_expr = None
            if idx < len(arguments.kw_defaults):
                default_expr = arguments.kw_defaults[idx]
            display, meta = build_param(
                arg, kind="keyword_only", default_expr=default_expr
            )
            parts.append(display)
            params_meta.append(meta)

        # var keyword
        if arguments.kwarg:
            display, meta = build_param(arguments.kwarg, kind="var_keyword")
            parts.append("**" + display)
            params_meta.append(meta)

        signature = "(" + ", ".join(p for p in parts if p) + ")"
        return signature, params_meta
