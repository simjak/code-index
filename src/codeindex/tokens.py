from __future__ import annotations
import re

_SPLIT_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def split_ident(name: str) -> list[str]:
    parts = []
    for seg in re.split(r"[^A-Za-z0-9]+", name):
        if not seg:
            continue
        camel = _SPLIT_CAMEL.sub(" ", seg).split()
        parts.extend(camel)
    return [p.lower() for p in parts if p]


def tokenize(text: str) -> list[str]:
    text = re.sub(r"[^A-Za-z0-9_]+", " ", text)
    toks = []
    for tok in text.split():
        toks.extend(split_ident(tok))
    return [t for t in toks if t]
