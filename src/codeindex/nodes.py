from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, NotRequired, TypedDict


class NodeKind(str, Enum):
    REPO = "repo"
    PKG = "pkg"
    FILE = "file"
    CLASS = "class"
    FUNC = "func"
    CONST = "const"
    BLOCK = "block"


class FunctionParam(TypedDict, total=False):
    name: str
    annotation: NotRequired[str | None]
    default: NotRequired[str | None]
    kind: NotRequired[str]


class FunctionDocMetadata(TypedDict, total=False):
    lang: Literal["python", "typescript", "javascript"]
    params: list[FunctionParam]
    returns: NotRequired[str | None]
    raises: NotRequired[list[str]]
    decorators: NotRequired[list[str]]
    visibility: NotRequired[str]
    is_async: NotRequired[bool]
    is_method: NotRequired[bool]
    owner: NotRequired[str | None]
    docstring: NotRequired[str | None]
    flags: NotRequired[dict[str, bool]]


class ClassDocMetadata(TypedDict, total=False):
    lang: Literal["python", "typescript", "javascript"]
    bases: NotRequired[list[str]]
    decorators: NotRequired[list[str]]
    visibility: NotRequired[str]
    docstring: NotRequired[str | None]


class FileDocMetadata(TypedDict, total=False):
    lang: Literal["python", "typescript", "javascript"]
    docstring: NotRequired[str | None]


class NodeExtra(TypedDict, total=False):
    doc: NotRequired[FunctionDocMetadata | ClassDocMetadata | FileDocMetadata]
    annotations: NotRequired[dict[str, Any]]


class CallsiteRef(TypedDict, total=False):
    type: Literal["node_id", "unresolved"]
    value: str
    symbol: NotRequired[str]
    reason: NotRequired[str]


class CallsiteRecord(TypedDict):
    caller_id: str
    callee_ref: CallsiteRef
    file: str
    line: int | None
    snippet: str | None


@dataclass
class Node:
    node_id: str
    parent_id: str | None
    kind: NodeKind
    path: str
    lang: str = "python"
    symbol: str | None = None
    signature: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    loc: int | None = None
    summary: str | None = None
    hash: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
