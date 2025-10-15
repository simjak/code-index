from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NodeKind(str, Enum):
    REPO = "repo"
    PKG = "pkg"
    FILE = "file"
    CLASS = "class"
    FUNC = "func"
    CONST = "const"
    BLOCK = "block"


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
