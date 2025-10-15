from __future__ import annotations

import ast
import hashlib
import os
import textwrap

from .nodes import Node, NodeKind


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
    def __init__(self, rel_path: str, file_text: str):
        self.rel_path = rel_path
        self.text = file_text
        self.nodes: list[Node] = []
        self.edges: list[dict] = []
        self.stack: list[str] = []
        self.defined_symbols: dict[str, str] = {}
        self.calls: dict[str, set[str]] = {}

    def parent_id(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def _register(
        self,
        kind: NodeKind,
        name: str | None,
        node: ast.AST,
        summary: str | None,
        signature: str | None = None,
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
        self.nodes.append(n)
        if name:
            self.defined_symbols[name] = n.node_id
        return n

    def index(self) -> tuple[list[Node], list[dict]]:
        tree = ast.parse(self.text)
        mod_doc = ast.get_docstring(tree)
        fnode = Node(
            node_id=stable_id(
                "file", self.rel_path, None, 1, len(self.text.splitlines())
            ),
            parent_id=None,
            kind=NodeKind.FILE,
            path=self.rel_path,
            lang="python",
            symbol=os.path.basename(self.rel_path),
            start_line=1,
            end_line=len(self.text.splitlines()),
            loc=len(self.text.splitlines()),
            summary=first_line(mod_doc),
            hash=hashlib.sha1(self.text.encode()).hexdigest(),
        )
        self.nodes.append(fnode)
        self.stack.append(fnode.node_id)
        self.generic_visit(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.edges.append(
                        {
                            "src": fnode.node_id,
                            "dst": a.name,
                            "type": "import",
                            "detail": a.asname or a.name,
                        }
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for a in node.names:
                    qual = f"{mod}.{a.name}" if mod else a.name
                    self.edges.append(
                        {
                            "src": fnode.node_id,
                            "dst": qual,
                            "type": "import",
                            "detail": a.asname or a.name,
                        }
                    )
        for fn_id, names in self.calls.items():
            for nm in sorted(names):
                dst = self.defined_symbols.get(nm, nm)
                self.edges.append(
                    {"src": fn_id, "dst": dst, "type": "call", "detail": nm}
                )
        self.stack.pop()
        return self.nodes, self.edges

    def visit_ClassDef(self, node: ast.ClassDef):
        doc = ast.get_docstring(node)
        cnode = self._register(NodeKind.CLASS, node.name, node, doc)
        for b in node.bases:
            bn = None
            if isinstance(b, ast.Name):
                bn = b.id
            elif isinstance(b, ast.Attribute):
                bn = b.attr
            elif isinstance(b, ast.Subscript):
                if isinstance(b.value, ast.Name):
                    bn = b.value.id
            if bn:
                dst = self.defined_symbols.get(bn, bn)
                self.edges.append(
                    {"src": cnode.node_id, "dst": dst, "type": "inherit", "detail": bn}
                )
        self.stack.append(cnode.node_id)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = "(" + ", ".join([a.arg for a in child.args.args]) + ")"
                md = ast.get_docstring(child)
                mnode = self._register(
                    NodeKind.BLOCK, child.name, child, md, signature=sig
                )
                self._collect_calls(mnode.node_id, child)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        sig = "(" + ", ".join([a.arg for a in node.args.args]) + ")"
        doc = ast.get_docstring(node)
        fnode = self._register(NodeKind.FUNC, node.name, node, doc, signature=sig)
        self._collect_calls(fnode.node_id, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self.visit_FunctionDef(node)

    def visit_Assign(self, node: ast.Assign):
        if len(self.stack) == 1:
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) >= 2:
                    self._register(NodeKind.CONST, t.id, node, summary=None)

    def _collect_calls(self, func_id: str, node: ast.AST):
        names: set[str] = set()

        class V(ast.NodeVisitor):
            def visit_Call(self, call):
                nm = None
                if isinstance(call.func, ast.Name):
                    nm = call.func.id
                elif isinstance(call.func, ast.Attribute):
                    nm = call.func.attr
                if nm:
                    names.add(nm)
                self.generic_visit(call)

        V().visit(node)
        self.calls[func_id] = names
