from __future__ import annotations

import os
from dataclasses import dataclass

from tree_sitter import Parser

try:
    from tree_sitter_languages import get_language
except Exception:
    get_language = None  # user must install tree_sitter_languages

from .ast_indexer import DEFAULT_CALLSITE_CAP, stable_id
from .logger import logger
from .nodes import (
    CallsiteRecord,
    CallsiteRef,
    FunctionDocMetadata,
    FunctionParam,
    Node,
    NodeKind,
)

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
        self.edges: list[dict] = []
        self.stack: list[str] = []
        self.defined: dict[str, str] = {}
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
        self.lang_name: str = ""
        self.class_stack: list[str] = []
        self.func_nodes: dict[str, Node] = {}

    def _parent_id(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def index(
        self,
    ) -> tuple[list[Node], list[dict], list[CallsiteRecord], dict[str, int]]:
        ext = os.path.splitext(self.rel_path)[1].lower()
        lang, lang_name = _get_language_for_ext(ext)
        if not lang:
            raise RuntimeError(f"No tree-sitter language available for extension {ext}")
        parser = Parser()
        parser.set_language(lang)
        tree = parser.parse(bytes(self.text, "utf-8"))
        root = tree.root_node
        self.lang_name = lang_name or "typescript"

        # File node
        total_lines = len(self.text.splitlines())
        extra = {}
        if self.enrich:
            extra = {"doc": {"lang": self.lang_name}}
        fnode = Node(
            node_id=stable_id("file", self.rel_path, None, 1, total_lines),
            parent_id=None,
            kind=NodeKind.FILE,
            path=self.rel_path,
            lang="typescript" if "ts" in ext else "javascript",
            symbol=os.path.basename(self.rel_path),
            start_line=1,
            end_line=total_lines,
            loc=total_lines,
            summary=None,
            hash=None,
            extra=extra,
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
        return self.nodes, self.edges, self.callsites, self.stats

    # ---- Walkers ----
    def _walk(self, node, lang_name: str):
        stack = [node]
        while stack:
            cur = stack.pop()
            t = cur.type

            if t == "class_declaration":
                name = None
                for ch in cur.children:
                    if ch.type in ("identifier", "type_identifier"):
                        name = _id_text(self.text, ch)
                        break
                start = cur.start_point[0] + 1
                end = cur.end_point[0] + 1
                extra = {}
                if self.enrich:
                    extra = {
                        "doc": {
                            "lang": self.lang_name or lang_name,
                            "visibility": self._visibility(name or ""),
                        }
                    }
                class_node = Node(
                    node_id=stable_id("class", self.rel_path, name, start, end),
                    parent_id=self._parent_id(),
                    kind=NodeKind.CLASS,
                    path=self.rel_path,
                    symbol=name,
                    start_line=start,
                    end_line=end,
                    loc=end - start + 1,
                    summary=None,
                    extra=extra,
                )
                self.nodes.append(class_node)
                if name:
                    self.defined[name] = class_node.node_id
                body = None
                for ch in cur.children:
                    if ch.type in ("class_body", "declaration_list"):
                        body = ch
                        break
                if body:
                    owner = name or None
                    for md in body.children:
                        if md.type in ("method_definition", "method_signature"):
                            mname = None
                            for c2 in md.children:
                                if c2.type in ("property_identifier", "identifier"):
                                    mname = _id_text(self.text, c2)
                                    break
                            if not mname:
                                continue
                            self._create_function_node(
                                name=mname,
                                ts_node=md,
                                span_node=md,
                                parent_id=class_node.node_id,
                                kind=NodeKind.BLOCK,
                                is_method=True,
                                owner=owner,
                                is_async=self._ts_is_async(md),
                                is_generator=self._ts_is_generator(md),
                            )
                # continue traversal
            elif t in ("function_declaration", "generator_function_declaration"):
                name = None
                for ch in cur.children:
                    if ch.type == "identifier":
                        name = _id_text(self.text, ch)
                        break
                self._create_function_node(
                    name=name,
                    ts_node=cur,
                    span_node=cur,
                    parent_id=self._parent_id(),
                    kind=NodeKind.FUNC,
                    is_method=False,
                    owner=None,
                    is_async=self._ts_is_async(cur),
                    is_generator=self._ts_is_generator(cur),
                )
            elif t in ("lexical_declaration", "variable_declaration"):
                for ch in cur.children:
                    if ch.type == "variable_declarator":
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
                            self._create_function_node(
                                name=name,
                                ts_node=init_node,
                                span_node=ch,
                                parent_id=self._parent_id(),
                                kind=NodeKind.FUNC,
                                is_method=False,
                                owner=None,
                                is_async=self._ts_is_async(init_node),
                                is_generator=self._ts_is_generator(init_node),
                            )
                        elif name and name.isupper():
                            start = ch.start_point[0] + 1
                            end = ch.end_point[0] + 1
                            const_node = Node(
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
                            self.nodes.append(const_node)
                            self.defined[name] = const_node.node_id

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
        call_edges: dict[str, set[str]] = {}
        call_records: dict[str, list[CallsiteRecord]] = {}

        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "call_expression":
                edge_symbol, display_symbol = self._call_symbols(node)
                caller_id = self._enclosing_func_id(node)
                if caller_id and (edge_symbol or display_symbol):
                    symbol_key = edge_symbol or display_symbol
                    call_edges.setdefault(caller_id, set()).add(symbol_key)
                    if self.enrich:
                        record = self._build_call_record(
                            caller_id, edge_symbol, display_symbol, node
                        )
                        if record:
                            call_records.setdefault(caller_id, []).append(record)
            for child in node.children:
                stack.append(child)

        for fid, names in call_edges.items():
            for nm in sorted(names):
                dst = self.defined.get(nm, nm)
                self.edges.append(
                    {"src": fid, "dst": dst, "type": "call", "detail": nm}
                )

        if not self.enrich:
            return

        for fid, records in call_records.items():
            capped = records[: self.call_cap]
            if len(records) > self.call_cap:
                self.stats["callsite_cap_hits"] += 1
                logger.debug(
                    "DEBUG: callsite cap hit for %s (%d>%d)",
                    fid,
                    len(records),
                    self.call_cap,
                )
            self.callsites.extend(capped)
            self.stats["callsites_total"] += len(capped)

    # ------------------------------------------------------------------

    def _create_function_node(
        self,
        *,
        name: str | None,
        ts_node,
        span_node,
        parent_id: str | None,
        kind: NodeKind,
        is_method: bool,
        owner: str | None,
        is_async: bool,
        is_generator: bool,
    ) -> None:
        start = span_node.start_point[0] + 1
        end = span_node.end_point[0] + 1
        signature, params_meta = self._extract_ts_params(ts_node)
        node = Node(
            node_id=stable_id(
                "block" if kind == NodeKind.BLOCK else "func",
                self.rel_path,
                name,
                start,
                end,
            ),
            parent_id=parent_id,
            kind=kind,
            path=self.rel_path,
            symbol=name,
            start_line=start,
            end_line=end,
            loc=end - start + 1,
            signature=signature,
        )
        self.nodes.append(node)
        if name:
            self.defined[name] = node.node_id
            if owner:
                self.defined[f"{owner}.{name}"] = node.node_id
        self.func_nodes[node.node_id] = node

        self.stats["funcs_total"] += 1

        if self.enrich:
            meta: FunctionDocMetadata = {
                "lang": self.lang_name
                or ("typescript" if "ts" in self.rel_path else "javascript"),
                "params": params_meta,
                "visibility": self._visibility(name or ""),
                "is_async": is_async,
                "flags": {"async": is_async, "generator": is_generator},
            }
            if is_method:
                meta["is_method"] = True
            if owner:
                meta["owner"] = owner
            self.stats["funcs_with_params"] += 1
            node.extra = {"doc": meta}

    def _extract_ts_params(self, ts_node) -> tuple[str, list[FunctionParam]]:
        params_node = None
        for child in ts_node.children:
            if child.type in (
                "formal_parameters",
                "parameters",
                "call_signature",
                "parameter_list",
            ):
                params_node = child
                break
        parts: list[str] = []
        if params_node is None:
            if ts_node.type == "arrow_function":
                for child in ts_node.children:
                    if child.type in ("identifier", "array_pattern", "object_pattern"):
                        parts = [_slice(self.text, child).strip()]
                        break
            else:
                parts = []
        else:
            raw = _slice(self.text, params_node).strip()
            if params_node.type == "identifier":
                parts = [raw]
            else:
                inner = raw
                if inner.startswith("(") and inner.endswith(")"):
                    inner = inner[1:-1]
                parts = [p.strip() for p in inner.split(",") if p.strip()]

        params_meta: list[FunctionParam] = []
        parsed_parts: list[str] = []
        for part in parts:
            text = part.strip()
            default = None
            if "=" in text:
                before, after = text.split("=", 1)
                text = before.strip()
                default = after.strip()
            annotation = None
            if ":" in text:
                base, annot = text.split(":", 1)
                text = base.strip()
                annotation = annot.strip()
            optional = text.endswith("?")
            if optional:
                text = text[:-1].strip()
            is_rest = text.startswith("...")
            if is_rest:
                text = text[3:].strip()
            name = text or part.strip()
            meta: FunctionParam = {"name": name, "kind": "rest" if is_rest else "param"}
            if annotation:
                meta["annotation"] = annotation
            if default is not None:
                meta["default"] = default
            params_meta.append(meta)
            cleaned = part.strip()
            parsed_parts.append(cleaned)

        inner_sig = ", ".join(parsed_parts)
        signature = f"({inner_sig})" if inner_sig else "()"
        return signature, params_meta

    def _visibility(self, name: str) -> str:
        if name.startswith("__") and not name.endswith("__"):
            return "private"
        if name.startswith("_"):
            return "protected"
        return "public"

    def _ts_is_async(self, ts_node) -> bool:
        snippet = _slice(self.text, ts_node).strip().lower()
        if snippet.startswith("async ") or snippet.startswith("async("):
            return True
        for child in ts_node.children:
            if getattr(child, "type", "") == "async":
                return True
        return False

    def _ts_is_generator(self, ts_node) -> bool:
        if "generator" in ts_node.type:
            return True
        snippet = _slice(self.text, ts_node).strip()
        return "function*" in snippet

    def _call_symbols(self, node) -> tuple[str | None, str | None]:
        if node.child_count == 0:
            return None, None
        fn = node.children[0]
        if fn.type == "identifier":
            text = _id_text(self.text, fn)
            return text, text
        if fn.type == "member_expression":
            prop = None
            for ch in fn.children[::-1]:
                if ch.type in ("property_identifier", "identifier"):
                    prop = _id_text(self.text, ch)
                    break
            display = _id_text(self.text, fn)
            return prop, display
        return None, None

    def _enclosing_func_id(self, node) -> str | None:
        row = node.start_point[0] + 1
        best: Node | None = None
        for nd in self.nodes:
            if (
                nd.kind in (NodeKind.FUNC, NodeKind.BLOCK)
                and nd.start_line
                and nd.end_line
            ):
                if nd.start_line <= row <= nd.end_line:
                    span = nd.end_line - nd.start_line
                    if best is None or span < (best.end_line - best.start_line):
                        best = nd
        return best.node_id if best else None

    def _build_call_record(
        self,
        caller_id: str,
        edge_symbol: str | None,
        display_symbol: str | None,
        node,
    ) -> CallsiteRecord | None:
        symbol_hint = display_symbol or edge_symbol
        if symbol_hint is None:
            return None
        resolved_id = None
        if edge_symbol and edge_symbol in self.defined:
            resolved_id = self.defined[edge_symbol]
        elif display_symbol and display_symbol in self.defined:
            resolved_id = self.defined[display_symbol]
        line = node.start_point[0] + 1
        snippet = _slice(self.text, node).strip()
        if resolved_id is None:
            logger.debug(
                "DEBUG: unresolved callee %s in %s:%d (%s)",
                symbol_hint,
                self.rel_path,
                line,
                "not_defined_in_file",
            )
            ref: CallsiteRef = {
                "type": "unresolved",
                "value": symbol_hint,
                "symbol": symbol_hint,
                "reason": "not_defined_in_file",
            }
        else:
            ref = {
                "type": "node_id",
                "value": resolved_id,
            }
            if symbol_hint:
                ref["symbol"] = symbol_hint  # type: ignore[index]
        return {
            "caller_id": caller_id,
            "callee_ref": ref,
            "file": self.rel_path,
            "line": line,
            "snippet": snippet,
        }
