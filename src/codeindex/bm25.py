from __future__ import annotations

import json
import math
import os

from .tokens import tokenize


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.N = 0
        self.avgdl = 0.0
        self.df: dict[str, int] = {}
        self.docs: dict[str, dict] = {}

    def add_doc(self, doc_id: str, text: str, limit_terms: int | None = 300):
        tokens = tokenize(text)
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        if limit_terms is not None and len(tf) > limit_terms:
            tf = dict(
                sorted(tf.items(), key=lambda kv: kv[1], reverse=True)[:limit_terms]
            )
        dl = sum(tf.values())
        self.docs[doc_id] = {"dl": dl, "tf": tf}
        self.N += 1

    def finalize(self):
        total = 0
        df: dict[str, int] = {}
        for doc in self.docs.values():
            total += doc["dl"]
            for t in doc["tf"].keys():
                df[t] = df.get(t, 0) + 1
        self.df = df
        self.avgdl = (total / self.N) if self.N else 0.0

    def idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log((self.N - n + 0.5) / (n + 0.5) + 1.0)

    def score(self, doc_id: str, query_terms: list[str]) -> float:
        doc = self.docs.get(doc_id)
        if not doc:
            return 0.0
        score = 0.0
        dl = doc["dl"] or 1
        for t in query_terms:
            tf = doc["tf"].get(t, 0)
            if tf == 0:
                continue
            idf = self.idf(t)
            denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            score += idf * (tf * (self.k1 + 1)) / denom
        return score

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        q = tokenize(query)
        cand = []
        terms = set(q)
        for doc_id, doc in self.docs.items():
            if terms & doc["tf"].keys():
                cand.append(doc_id)
        scored = [(d, self.score(d, q)) for d in cand]
        scored = [s for s in scored if s[1] > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def save(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "k1": self.k1,
                    "b": self.b,
                    "N": self.N,
                    "avgdl": self.avgdl,
                    "df": self.df,
                    "docs": self.docs,
                },
                f,
            )

    @classmethod
    def load(cls, path: str) -> "BM25Index":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        obj = cls(k1=data.get("k1", 1.5), b=data.get("b", 0.75))
        obj.N = data["N"]
        obj.avgdl = data["avgdl"]
        obj.df = data["df"]
        obj.docs = data["docs"]
        return obj
