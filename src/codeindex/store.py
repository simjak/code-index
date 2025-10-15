from __future__ import annotations
import os
import json
from dataclasses import asdict
from typing import Iterable
from .nodes import Node


def write_jsonl(path: str, items: Iterable[dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for obj in items:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_nodes(path: str, nodes: Iterable[Node]):
    write_jsonl(path, (asdict(n) for n in nodes))


def write_edges(path: str, edges: Iterable[dict]):
    write_jsonl(path, edges)
