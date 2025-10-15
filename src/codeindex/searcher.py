from __future__ import annotations

import heapq
import json
import os
from dataclasses import dataclass

from .bm25 import BM25Index
from .store import load_jsonl


@dataclass
class TraceStep:
    event: str
    node_id: str
    score: float
    reason: str
    meta: dict


def load_nodes(index_dir: str) -> dict:
    nodes = {}
    for n in load_jsonl(os.path.join(index_dir, "nodes.jsonl")):
        nodes[n["node_id"]] = n
    return nodes


def parent_index(nodes: dict) -> dict:
    return {nid: n["parent_id"] for nid, n in nodes.items()}


def children_index(nodes: dict) -> dict:
    ch: dict[str, list] = {}
    for nid, n in nodes.items():
        p = n["parent_id"]
        if p is not None:
            ch.setdefault(p, []).append(nid)
    return ch


def aggregate_desc_scores(node_id: str, children: dict, local_scores: dict) -> float:
    total = local_scores.get(node_id, 0.0)
    for c in children.get(node_id, []):
        total += aggregate_desc_scores(c, children, local_scores)
    return total


def search(
    index_dir: str, query: str, *, top: int = 10, budget: int = 120, gate: str = "off"
) -> dict:
    bm25 = BM25Index.load(os.path.join(index_dir, "bm25.json"))
    nodes = load_nodes(index_dir)
    parents = parent_index(nodes)
    children = children_index(nodes)
    top_bm25 = bm25.search(query, top_k=max(top * 20, 200))
    local_scores = {doc: sc for doc, sc in top_bm25}
    cand = set(local_scores.keys())
    for nid in list(cand):
        p = parents.get(nid)
        while p:
            cand.add(p)
            p = parents.get(p)

    def node_score(nid: str) -> float:
        n = nodes[nid]
        bonus = 0.0
        for w in query.lower().split():
            if (n.get("symbol") and w in (n["symbol"] or "").lower()) or (
                n.get("path") and w in (n["path"] or "").lower()
            ):
                bonus += 0.1
        return aggregate_desc_scores(nid, children, local_scores) + bonus

    frontier: list[tuple[float, str]] = []
    root = [nid for nid, n in nodes.items() if n["parent_id"] is None][0]
    heapq.heappush(frontier, (-node_score(root), root))
    visited = set()
    answers: list[tuple[str, float]] = []
    trace: list[dict] = []
    steps = 0
    while frontier and steps < budget:
        steps += 1
        score, nid = heapq.heappop(frontier)
        score = -score
        if nid in visited:
            continue
        visited.add(nid)
        n = nodes[nid]
        reason = f"agg(desc BM25)={score:.3f}; symbol={n.get('symbol')}; path={n.get('path')}"
        trace.append(
            TraceStep(
                event="expand",
                node_id=nid,
                score=score,
                reason=reason,
                meta={"kind": n["kind"]},
            ).__dict__
        )
        if n["kind"] in ("func", "block", "const"):
            answers.append((nid, local_scores.get(nid, 0.0)))
            trace.append(
                TraceStep(
                    event="answer",
                    node_id=nid,
                    score=local_scores.get(nid, 0.0),
                    reason="leaf candidate",
                    meta={},
                ).__dict__
            )
            continue
        kids = children.get(nid, [])
        kids.sort(key=lambda k: node_score(k), reverse=True)
        for k in kids[:10]:
            heapq.heappush(frontier, (-node_score(k), k))
    answers.sort(key=lambda x: x[1], reverse=True)
    results = [
        {
            "node_id": nid,
            "score": sc,
            "path": nodes[nid]["path"],
            "symbol": nodes[nid].get("symbol"),
            "kind": nodes[nid]["kind"],
        }
        for nid, sc in answers[:top]
    ]
    tdir = os.path.join(index_dir, "trace")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "last_trace.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"query": query, "results": results, "trace": trace},
            f,
            ensure_ascii=False,
            indent=2,
        )
    with open(os.path.join(tdir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    build_trace_html(index_dir)
    return {
        "results": results,
        "trace_path": os.path.join(tdir, "last_trace.json"),
        "html": os.path.join(tdir, "trace.html"),
    }


def build_trace_html(index_dir: str):
    tdir = os.path.join(index_dir, "trace")
    data_path = os.path.join(tdir, "last_trace.json")
    if not os.path.exists(data_path):
        return
    with open(data_path, "r", encoding="utf-8") as f:
        payload = f.read()
    html_parts = [
        "<!doctype html>\n",
        '<html><head><meta charset="utf-8"/><title>CodeIndex TraceView</title>\n',
        "<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:24px}"
        ".container{display:grid;grid-template-columns:320px 1fr;gap:16px}"
        ".panel{border:1px solid #ddd;border-radius:8px;padding:12px}"
        "h1{margin:0 0 12px 0;font-size:18px}"
        ".node{padding:6px 8px;border-radius:6px;margin-bottom:6px;cursor:pointer}"
        ".node:hover{background:#f2f4f8}"
        ".small{color:#666;font-size:12px}"
        ".badge{background:#eef;padding:2px 6px;border-radius:4px;margin-left:6px}"
        "pre{white-space:pre-wrap}</style>\n",
        '</head><body><div class="container"><div class="panel"><h1>Trace</h1><div id="trace"></div></div>\n',
        '<div class="panel"><h1>Details</h1><div id="details" class="small">Select a step to see details.</div><h1 style="margin-top:16px">Top Results</h1><ol id="results"></ol></div></div>\n',
        "<script>\n",
        "const DATA = ",
        payload,
        ";\n",
        "function escapeHtml(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}\n",
        "function el(t,c,h){const e=document.createElement(t); if(c)e.className=c; if(h)e.innerHTML=h; return e;}\n",
        "const T=document.getElementById('trace');\n",
        "DATA.trace.forEach((st,i)=>{const e=el('div','node',`<b>${i+1}. ${st.event}</b> <span class=\"badge\">${st.node_id.slice(0,8)}</span> <span class=\"small\">score=${(st.score||0).toFixed(3)}</span><br><span class=\"small\">${escapeHtml(st.reason||'')}</span>`); e.onclick=()=>{const d=el('div',null,`<b>Event:</b> ${st.event}<br><b>Node:</b> ${st.node_id}<br><b>Score:</b> ${(st.score||0).toFixed(4)}<br><b>Reason:</b><br><pre>${escapeHtml(st.reason||'')}</pre><br><b>Meta:</b><pre>${escapeHtml(JSON.stringify(st.meta||{},null,2))}</pre>`); const det=document.getElementById('details'); det.innerHTML=''; det.appendChild(d); }; T.appendChild(e);});\n",
        "const R=document.getElementById('results'); DATA.results.forEach(r=>{const e=el('li',null,`<code>${r.kind}</code> <b>${r.symbol||'(anon)'}</b> <span class=\"small\">${escapeHtml(r.path||'')}</span> <span class=\"badge\">${(r.score||0).toFixed(3)}</span>`); R.appendChild(e);});\n",
        "</script></body></html>",
    ]
    html = "".join(html_parts)
    with open(os.path.join(tdir, "trace.html"), "w", encoding="utf-8") as f:
        f.write(html)
