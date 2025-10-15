from __future__ import annotations

import os
from dataclasses import dataclass

from tree_sitter import Parser

try:
    from tree_sitter_languages import get_language
except Exception:
    get_language = None  # user must install tree_sitter_languages

from .ast_indexer import stable_id
from .nodes import Node, NodeKind

LANG_BY_EXT = {
    ".js": "javascript",
    ".jsx": "javascript",  # grammar usually has JSX enabled
    ".ts": "typescript",
    ".tsx": "tsx",  # if unavailable, we fall back to 'typescript'
}


def _get_language_for_ext(ext: str):
    if get_language is None:
        raise RuntimeError(
            "tree_sitter_languages is required for JS/TS parsing. Install with: uv add tree_sitter tree_sitter_languages"
        )
    lang_name = LANG_BY_EXT.get(ext.lower())
    if lang_name is None:
        return None
    try:
        return get_language(lang_name), lang_name
    except Exception:
        # fallbacks
        if lang_name == "tsx":
            return get_language("typescript"), "typescript"
        if lang_name == "javascript":
            return get_language("javascript"), "javascript"
        return None, None


def _slice(text: str, node) -> str:
    # node.start_point/end_point are (row, col), 0-based
    (sr, sc) = node.start_point
    (er, ec) = node.end_point
    lines = text.splitlines()
    seg = []
    for i in range(sr, er + 1):
        line = lines[i]
        if i == sr and i == er:
            seg.append(line[sc:ec])
        elif i == sr:
            seg.append(line[sc:])
        elif i == er:
            seg.append(line[:ec])
        else:
            seg.append(line)
    return "\n".join(seg)


def _node_text(text: str, node) -> str:
    return _slice(text, node)


def _id_text(text: str, node) -> str | None:
    if node is None:
        return None
    return _slice(text, node).strip()


@dataclass
class _Ctx:
    text: str
    nodes: list[Node]
    edges: list[dict]
    stack: list[str]
    defined: dict[str, str]


class TSFileIndexer:
    """TypeScript/JavaScript indexer via tree-sitter."""

    def __init__(self, rel_path: str, file_text: str):
        self.rel_path = rel_path
        self.text = file_text
        self.nodes: list[Node] = []
        self.edges: list[dict] = []
        self.stack: list[str] = []
        self.defined: dict[str, str] = {}

    def _parent_id(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def index(self) -> tuple[list[Node], list[dict]]:
        ext = os.path.splitext(self.rel_path)[1].lower()
        lang, lang_name = _get_language_for_ext(ext)
        if not lang:
            raise RuntimeError(f"No tree-sitter language available for extension {ext}")
        parser = Parser()
        parser.set_language(lang)
        tree = parser.parse(bytes(self.text, "utf-8"))
        root = tree.root_node

        # File node
        fnode = Node(
            node_id=stable_id(
                "file", self.rel_path, None, 1, len(self.text.splitlines())
            ),
            parent_id=None,
            kind=NodeKind.FILE,
            path=self.rel_path,
            lang="typescript" if "ts" in ext else "javascript",
            symbol=os.path.basename(self.rel_path),
            start_line=1,
            end_line=len(self.text.splitlines()),
            loc=len(self.text.splitlines()),
            summary=None,
            hash=None,
        )
        self.nodes.append(fnode)
        self.stack.append(fnode.node_id)

        # Walk and gather
        self._walk(root, lang_name)

        # imports
        self._collect_imports(root, lang_name)
        # calls
        self._collect_calls(root)

        self.stack.pop()
        return self.nodes, self.edges

    # ---- Walkers ----
    def _walk(self, node, lang_name: str):
        # Pre-order traversal
        stack = [node]
        while stack:
            cur = stack.pop()
            t = cur.type

            # Classes
            if t == "class_declaration":
                name = None
                for ch in cur.children:
                    if ch.type in ("identifier", "type_identifier"):
                        name = _id_text(self.text, ch)
                        break
                start = cur.start_point[0] + 1
                end = cur.end_point[0] + 1
                n = Node(
                    node_id=stable_id("class", self.rel_path, name, start, end),
                    parent_id=self._parent_id(),
                    kind=NodeKind.CLASS,
                    path=self.rel_path,
                    symbol=name,
                    start_line=start,
                    end_line=end,
                    loc=end - start + 1,
                    summary=None,
                )
                self.nodes.append(n)
                if name:
                    self.defined[name] = n.node_id
                # methods within body
                body = None
                for ch in cur.children:
                    if ch.type in ("class_body", "declaration_list"):
                        body = ch
                        break
                if body:
                    for md in body.children:
                        if md.type in ("method_definition", "method_signature"):
                            mname = None
                            # method_definition: child property_identifier or property name in member_expression
                            for c2 in md.children:
                                if c2.type in ("property_identifier", "identifier"):
                                    mname = _id_text(self.text, c2)
                                    break
                            if not mname:
                                # computed names: skip
                                continue
                            ms = md.start_point[0] + 1
                            me = md.end_point[0] + 1
                            mn = Node(
                                node_id=stable_id(
                                    "block", self.rel_path, mname, ms, me
                                ),
                                parent_id=n.node_id,
                                kind=NodeKind.BLOCK,
                                path=self.rel_path,
                                symbol=mname,
                                start_line=ms,
                                end_line=me,
                                loc=me - ms + 1,
                            )
                            self.nodes.append(mn)
                # continue traversal
            # Functions
            elif t in ("function_declaration", "generator_function_declaration"):
                name = None
                for ch in cur.children:
                    if ch.type == "identifier":
                        name = _id_text(self.text, ch)
                        break
                start = cur.start_point[0] + 1
                end = cur.end_point[0] + 1
                fn = Node(
                    node_id=stable_id("func", self.rel_path, name, start, end),
                    parent_id=self._parent_id(),
                    kind=NodeKind.FUNC,
                    path=self.rel_path,
                    symbol=name,
                    start_line=start,
                    end_line=end,
                    loc=end - start + 1,
                )
                self.nodes.append(fn)
                if name:
                    self.defined[name] = fn.node_id

            # Variable-declared functions / constants
            elif t in ("lexical_declaration", "variable_declaration"):
                # iterate declarators
                for ch in cur.children:
                    if ch.type == "variable_declarator":
                        # name
                        name_node = None
                        init_node = None
                        for x in ch.children:
                            if x.type in (
                                "identifier",
                                "array_pattern",
                                "object_pattern",
                            ):
                                name_node = x
                            elif x.type in (
                                "arrow_function",
                                "function",
                                "generator_function",
                                "function_expression",
                            ):
                                init_node = x
                        name = _id_text(self.text, name_node) if name_node else None
                        if name and init_node is not None:
                            start = ch.start_point[0] + 1
                            end = ch.end_point[0] + 1
                            fn = Node(
                                node_id=stable_id(
                                    "func", self.rel_path, name, start, end
                                ),
                                parent_id=self._parent_id(),
                                kind=NodeKind.FUNC,
                                path=self.rel_path,
                                symbol=name,
                                start_line=start,
                                end_line=end,
                                loc=end - start + 1,
                            )
                            self.nodes.append(fn)
                            self.defined[name] = fn.node_id
                        elif name and name.isupper():
                            start = ch.start_point[0] + 1
                            end = ch.end_point[0] + 1
                            cn = Node(
                                node_id=stable_id(
                                    "const", self.rel_path, name, start, end
                                ),
                                parent_id=self._parent_id(),
                                kind=NodeKind.CONST,
                                path=self.rel_path,
                                symbol=name,
                                start_line=start,
                                end_line=end,
                                loc=end - start + 1,
                            )
                            self.nodes.append(cn)
                            self.defined[name] = cn.node_id

            # Recurse
            for child in reversed(cur.children):
                stack.append(child)

    def _collect_imports(self, root, lang_name: str):
        stack = [root]
        while stack:
            n = stack.pop()
            if n.type == "import_statement" or n.type == "import_declaration":
                src = None
                for ch in n.children:
                    if ch.type in ("string", "string_literal"):
                        src = _id_text(self.text, ch).strip("\"'")
                if src:
                    self.edges.append(
                        {
                            "src": self.nodes[0].node_id,
                            "dst": src,
                            "type": "import",
                            "detail": src,
                        }
                    )
            for c in n.children:
                stack.append(c)

    def _collect_calls(self, root):
        # For simplicity, collect call_expression heads (identifier or member_expression.property)
        funcs: dict[str, set[str]] = {}

        # Map leaf methods to their node_id (roughly by nearest ancestor function/block)
        def enclosing_func_id(node) -> str | None:
            # Walk up to nearest FUNC or BLOCK we created
            # Here we approximate by location
            row = node.start_point[0] + 1
            best = None
            for nd in self.nodes:
                if (
                    nd.kind in (NodeKind.FUNC, NodeKind.BLOCK)
                    and nd.start_line
                    and nd.end_line
                ):
                    if nd.start_line <= row <= nd.end_line:
                        # prefer the tightest span
                        span = nd.end_line - nd.start_line
                        if best is None or span < (best.end_line - best.start_line):
                            best = nd
            return best.node_id if best else None

        stack = [root]
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                callee = None
                # child 0 is usually function, but traverse to find identifier/member_expression
                if n.child_count > 0:
                    fn = n.children[0]
                    if fn.type == "identifier":
                        callee = _id_text(self.text, fn)
                    elif fn.type == "member_expression":
                        # foo.bar -> use 'bar'
                        for ch in fn.children[::-1]:
                            if ch.type in ("property_identifier", "identifier"):
                                callee = _id_text(self.text, ch)
                                break
                if callee:
                    fid = enclosing_func_id(n)
                    if fid:
                        funcs.setdefault(fid, set()).add(callee)
            for c in n.children:
                stack.append(c)

        for fid, names in funcs.items():
            for nm in sorted(names):
                dst = self.defined.get(nm, nm)
                self.edges.append(
                    {"src": fid, "dst": dst, "type": "call", "detail": nm}
                )
