from __future__ import annotations

import asyncio
import heapq
import json
import os
from dataclasses import dataclass

from .bm25 import BM25Index
from .llm_search import llm_guided_search
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
            # Store both local BM25 score and aggregate score for better ranking
            local_bm25 = local_scores.get(nid, 0.0)
            agg_score = node_score(nid)
            # Use hybrid score: prioritize direct matches, but use aggregate for context
            hybrid_score = local_bm25 * 2.0 + agg_score
            answers.append((nid, local_bm25, agg_score, hybrid_score))
            trace.append(
                TraceStep(
                    event="answer",
                    node_id=nid,
                    score=local_bm25,
                    reason=f"leaf candidate; local={local_bm25:.3f} agg={agg_score:.3f}",
                    meta={"aggregate_score": agg_score},
                ).__dict__
            )
            continue
        kids = children.get(nid, [])
        kids.sort(key=lambda k: node_score(k), reverse=True)
        for k in kids[:10]:
            heapq.heappush(frontier, (-node_score(k), k))
    # Sort by hybrid score: direct matches + hierarchical context
    answers.sort(key=lambda x: x[3], reverse=True)
    results = [
        {
            "node_id": nid,
            "score": local_bm25,
            "aggregate_score": agg_score,
            "hybrid_score": hybrid_score,
            "path": nodes[nid]["path"],
            "symbol": nodes[nid].get("symbol"),
            "kind": nodes[nid]["kind"],
        }
        for nid, local_bm25, agg_score, hybrid_score in answers[:top]
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


def build_trace_html(index_dir: str) -> bool:
    """Generate enhanced HTML trace viewer with LLM reasoning visualization."""
    tdir = os.path.join(index_dir, "trace")
    data_path = os.path.join(tdir, "last_trace.json")
    if not os.path.exists(data_path):
        return False

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Detect search mode
    mode = data.get("mode", "bm25")
    stats = data.get("stats", {})

    # Load full nodes and edges for enhanced visualization
    nodes_file = os.path.join(index_dir, "nodes.jsonl")
    edges_file = os.path.join(index_dir, "edges.jsonl")

    nodes_data = []
    edges_data = []

    if os.path.exists(nodes_file):
        with open(nodes_file, "r", encoding="utf-8") as f:
            nodes_data = [json.loads(line) for line in f if line.strip()]

    if os.path.exists(edges_file):
        with open(edges_file, "r", encoding="utf-8") as f:
            edges_data = [json.loads(line) for line in f if line.strip()]

    html = (
        """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>CodeIndex - Advanced Code Explorer</title>
<style>
* { box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    margin: 0;
    padding: 0;
    background: #f5f7fa;
    overflow: hidden;
}
.header {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    padding: 20px 30px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
}
.header h1 {
    margin: 0 0 10px 0;
    font-size: 28px;
    font-weight: 700;
}
.stats {
    display: flex;
    gap: 20px;
    margin-top: 12px;
}
.stat {
    padding: 8px 16px;
    background: rgba(255,255,255,0.2);
    border-radius: 20px;
    font-size: 13px;
    backdrop-filter: blur(10px);
}
.stat strong { font-weight: 700; }

.tabs {
    display: flex;
    gap: 0;
    background: white;
    border-bottom: 2px solid #e0e0e0;
    padding: 0 30px;
}
.tab {
    padding: 15px 25px;
    cursor: pointer;
    border: none;
    background: none;
    font-size: 15px;
    font-weight: 600;
    color: #666;
    border-bottom: 3px solid transparent;
    transition: all 0.3s;
}
.tab:hover { color: #667eea; }
.tab.active {
    color: #667eea;
    border-bottom-color: #667eea;
}

.content {
    height: calc(100vh - 180px);
    overflow: hidden;
}
.view {
    display: none;
    height: 100%;
    padding: 20px 30px;
    overflow-y: auto;
}
.view.active { display: block; }

/* Tree View */
.tree-search {
    margin-bottom: 15px;
    padding: 10px 15px;
    border: 2px solid #e0e0e0;
    border-radius: 8px;
    width: 100%;
    font-size: 14px;
}
.tree-container {
    background: white;
    border-radius: 8px;
    padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
.tree-node {
    padding: 6px 10px;
    margin: 2px 0;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.2s;
    user-select: none;
}
.tree-node:hover { background: #f0f4ff; }
.tree-node.explored { background: #e3f2fd; }
.tree-node.answer {
    background: #c8e6c9;
    font-weight: 600;
}
.node-header {
    display: flex;
    align-items: center;
    gap: 8px;
}
.toggle {
    display: inline-block;
    width: 16px;
    text-align: center;
    font-size: 10px;
    color: #999;
}
.children {
    margin-left: 20px;
    border-left: 1px dashed #ddd;
    padding-left: 5px;
}

/* Reasoning Tree View */
.reasoning-tree {
    background: white;
    border-radius: 8px;
    padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    overflow-x: auto;
}
.reason-node {
    position: relative;
    padding: 12px 16px;
    margin: 10px;
    border-radius: 8px;
    border-left: 4px solid #ddd;
    background: #f9f9f9;
    min-width: 250px;
    display: inline-block;
}
.reason-node.evaluate { border-left-color: #17a2b8; }
.reason-node.answer { border-left-color: #28a745; background: #e8f5e9; }
.reason-node.expand { border-left-color: #ffc107; }
.reason-header {
    display: flex;
    justify-content: space-between;
    margin-bottom: 6px;
}
.reason-symbol {
    font-weight: 600;
    font-size: 14px;
}
.reason-confidence {
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
}
.conf-high { background: #d4edda; color: #155724; }
.conf-med { background: #fff3cd; color: #856404; }
.conf-low { background: #f8d7da; color: #721c24; }
.reason-path {
    font-size: 11px;
    color: #666;
    margin-bottom: 6px;
}
.reason-text {
    font-size: 12px;
    color: #555;
    font-style: italic;
}

/* Impact Map */
.impact-container {
    background: white;
    border-radius: 8px;
    padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
#impact-canvas {
    width: 100%;
    height: 600px;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
}
.impact-legend {
    display: flex;
    gap: 20px;
    margin-top: 15px;
    padding: 15px;
    background: #f9f9f9;
    border-radius: 8px;
}
.legend-item {
    display: flex;
    align-items: center;
    gap: 8px;
}
.legend-color {
    width: 20px;
    height: 20px;
    border-radius: 4px;
}

/* Architecture View */
.arch-container {
    background: white;
    border-radius: 8px;
    padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
#arch-canvas {
    width: 100%;
    height: 600px;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
}
.component-box {
    padding: 15px;
    border-radius: 8px;
    margin: 10px;
    display: inline-block;
    box-shadow: 0 2px 6px rgba(0,0,0,0.1);
}
.component-title {
    font-weight: 700;
    font-size: 14px;
    margin-bottom: 8px;
}
.component-items {
    font-size: 12px;
    color: #666;
}

/* Results Panel */
.results-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 15px;
}
.result-card {
    background: white;
    border-radius: 8px;
    padding: 15px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.08);
    border-left: 4px solid #28a745;
    transition: transform 0.2s;
}
.result-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
.result-symbol {
    font-weight: 600;
    font-size: 15px;
    color: #333;
    margin-bottom: 6px;
}
.result-kind {
    display: inline-block;
    padding: 2px 8px;
    background: #e3f2fd;
    border-radius: 4px;
    font-size: 11px;
    margin-right: 6px;
}
.result-path {
    font-size: 12px;
    color: #666;
    margin: 6px 0;
}
.result-reasoning {
    font-size: 13px;
    color: #555;
    margin-top: 10px;
    font-style: italic;
    border-top: 1px solid #f0f0f0;
    padding-top: 10px;
}
</style>
</head>
<body>
<div class="header">
    <h1>🔍 """
        + data.get("query", "Search Query")
        + """</h1>
    <div class="stats">
        <div class="stat">Mode: <strong>"""
        + mode.upper()
        + """</strong></div>
        <div class="stat">Steps: <strong>"""
        + str(stats.get("steps", len(data.get("trace", []))))
        + """</strong></div>
        <div class="stat">Results: <strong>"""
        + str(len(data.get("results", [])))
        + """</strong></div>
        """
        + (
            f'<div class="stat">Budget: <strong>{stats.get("budget", "?")}</strong></div>'
            if mode == "llm"
            else ""
        )
        + """
    </div>
</div>

<div class="tabs">
    <button class="tab active" onclick="switchTab('tree')">🌳 Code Structure</button>
    <button class="tab" onclick="switchTab('reasoning')">🧠 Reasoning Tree</button>
    <button class="tab" onclick="switchTab('impact')">🔗 Impact Map</button>
    <button class="tab" onclick="switchTab('architecture')">🏗️ Architecture</button>
    <button class="tab" onclick="switchTab('results')">✅ Results</button>
</div>

<div class="content">
    <div id="tree-view" class="view active">
        <input type="text" class="tree-search" placeholder="🔍 Search code structure..." onkeyup="searchTree(event)"/>
        <div class="tree-container" id="tree-container"></div>
    </div>
    
    <div id="reasoning-view" class="view">
        <div class="reasoning-tree" id="reasoning-tree"></div>
    </div>
    
    <div id="impact-view" class="view">
        <div class="impact-container">
            <h2 style="margin-top:0">Call Graph & Dependencies</h2>
            <canvas id="impact-canvas"></canvas>
            <div class="impact-legend">
                <div class="legend-item">
                    <div class="legend-color" style="background:#667eea"></div>
                    <span>Function/Class</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background:#28a745"></div>
                    <span>Answer Node</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background:#ffc107"></div>
                    <span>Explored Node</span>
                </div>
            </div>
        </div>
    </div>
    
    <div id="architecture-view" class="view">
        <div class="arch-container">
            <h2 style="margin-top:0">System Architecture</h2>
            <canvas id="arch-canvas"></canvas>
        </div>
    </div>
    
    <div id="results-view" class="view">
        <h2 style="margin-top:0;margin-bottom:20px">Search Results</h2>
        <div class="results-grid" id="results-grid"></div>
    </div>
</div>

<script>
const DATA = """
        + json.dumps(data)
        + """;
const NODES = """
        + json.dumps(nodes_data)
        + """;
const EDGES = """
        + json.dumps(edges_data)
        + """;

let exploredNodes = new Set();
let answerNodes = new Set();

// Extract explored and answer nodes from trace
DATA.trace?.forEach(st => {
    if (st.event === 'evaluate' || st.event === 'expand') {
        exploredNodes.add(st.node_id);
    }
    if (st.is_answer || st.event === 'answer') {
        answerNodes.add(st.node_id);
    }
});

function switchTab(view) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById(view + '-view').classList.add('active');
    
    // Render on first view
    if (view === 'reasoning' && !document.getElementById('reasoning-tree').innerHTML) {
        renderReasoningTree();
    }
    if (view === 'impact' && !document.getElementById('impact-canvas').dataset.rendered) {
        renderImpactMap();
    }
    if (view === 'architecture' && !document.getElementById('arch-canvas').dataset.rendered) {
        renderArchitecture();
    }
}

function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
}

// ==================== CODE TREE ====================
function buildTreeStructure() {
    const nodeMap = {};
    NODES.forEach(n => { nodeMap[n.node_id] = {...n, children: []} });
    
    const root = NODES.find(n => !n.parent_id);
    if (!root) return null;
    
    NODES.forEach(n => {
        if (n.parent_id && nodeMap[n.parent_id]) {
            nodeMap[n.parent_id].children.push(nodeMap[n.node_id]);
        }
    });
    
    return nodeMap[root.node_id];
}

function renderTreeNode(node, container) {
    const div = document.createElement('div');
    div.className = 'tree-node';
    div.dataset.nodeId = node.node_id;
    
    if (exploredNodes.has(node.node_id)) div.classList.add('explored');
    if (answerNodes.has(node.node_id)) div.classList.add('answer');
    
    const icon = {
        'repo': '📦', 'pkg': '📁', 'file': '📄',
        'class': '🏛️', 'func': '⚡', 'const': '💎', 'block': '📦'
    }[node.kind] || '•';
    
    const header = document.createElement('div');
    header.className = 'node-header';
    
    const toggle = document.createElement('span');
    toggle.className = 'toggle';
    toggle.textContent = node.children.length > 0 ? '▼' : '';
    
    // FIX: Create proper closure for toggle handler
    if (node.children.length > 0) {
        toggle.style.cursor = 'pointer';
        toggle.addEventListener('click', function(e) {
            e.stopPropagation();
            const childrenDiv = div.querySelector('.children');
            if (childrenDiv) {
                const isCollapsed = childrenDiv.style.display === 'none';
                childrenDiv.style.display = isCollapsed ? 'block' : 'none';
                toggle.textContent = isCollapsed ? '▼' : '▶';
            }
        });
    }
    
    const labelSpan = document.createElement('span');
    labelSpan.innerHTML = `${icon} <strong>${escapeHtml(node.symbol || node.kind)}</strong>`;
    
    header.appendChild(toggle);
    header.appendChild(labelSpan);
    div.appendChild(header);
    
    if (node.children.length > 0) {
        const childContainer = document.createElement('div');
        childContainer.className = 'children';
        node.children.forEach(child => renderTreeNode(child, childContainer));
        div.appendChild(childContainer);
    }
    
    container.appendChild(div);
}

function searchTree(event) {
    const query = event.target.value.toLowerCase();
    const nodes = document.querySelectorAll('.tree-node');
    
    nodes.forEach(node => {
        const text = node.textContent.toLowerCase();
        if (text.includes(query)) {
            node.style.display = 'block';
            // Expand parents
            let parent = node.parentElement;
            while (parent) {
                if (parent.classList.contains('tree-node')) {
                    parent.classList.remove('collapsed');
                    const toggle = parent.querySelector('.toggle');
                    if (toggle) toggle.textContent = '▼';
                }
                parent = parent.parentElement;
            }
        } else if (query) {
            node.style.display = 'none';
        } else {
            node.style.display = 'block';
        }
    });
}

// ==================== REASONING TREE ====================
function renderReasoningTree() {
    const container = document.getElementById('reasoning-tree');
    const trace = DATA.trace || [];
    
    // Build tree structure from trace
    const treeData = [];
    const nodeMap = {};
    
    trace.forEach((step, i) => {
        const node = {
            id: step.node_id,
            step: i + 1,
            event: step.event,
            symbol: step.symbol || step.node_id?.slice(0, 8),
            path: step.path,
            confidence: step.confidence || step.score || 0,
            reasoning: step.reasoning,
            is_answer: step.is_answer || step.event === 'answer',
            children: []
        };
        
        nodeMap[step.node_id] = node;
        
        // Find parent based on expand events
        if (step.event === 'expand' && i > 0) {
            // This is a child being added to frontier
            const parentStep = trace.slice(0, i).reverse().find(s => s.event === 'evaluate');
            if (parentStep && nodeMap[parentStep.node_id]) {
                nodeMap[parentStep.node_id].children.push(node);
            } else {
                treeData.push(node);
            }
        } else if (step.event === 'evaluate') {
            treeData.push(node);
        }
    });
    
    // Render tree
    if (treeData.length > 0) {
        treeData.forEach(node => renderReasonNode(node, container, 0));
    } else {
        container.innerHTML = '<p style="color:#666">No reasoning trace available</p>';
    }
}

function renderReasonNode(node, container, depth) {
    const div = document.createElement('div');
    div.className = `reason-node ${node.event}`;
    div.style.marginLeft = (depth * 30) + 'px';
    
    const confClass = node.confidence > 0.6 ? 'conf-high' : node.confidence > 0.3 ? 'conf-med' : 'conf-low';
    
    div.innerHTML = `
        <div class="reason-header">
            <span class="reason-symbol">${node.step}. ${node.event === 'evaluate' ? '🔎' : node.event === 'answer' ? '✅' : '🔀'} ${escapeHtml(node.symbol)}</span>
            <span class="reason-confidence ${confClass}">${(node.confidence * 100).toFixed(0)}%</span>
        </div>
        <div class="reason-path">${escapeHtml(node.path || '')}</div>
        ${node.reasoning ? `<div class="reason-text">${escapeHtml(node.reasoning)}</div>` : ''}
        ${node.is_answer ? '<div style="color:#28a745;font-weight:600;margin-top:6px">✓ ANSWER</div>' : ''}
    `;
    
    container.appendChild(div);
    
    node.children.forEach(child => renderReasonNode(child, container, depth + 1));
}

// ==================== IMPACT MAP ====================
function renderImpactMap() {
    const canvas = document.getElementById('impact-canvas');
    canvas.dataset.rendered = 'true';
    const ctx = canvas.getContext('2d');
    canvas.width = canvas.offsetWidth;
    canvas.height = 600;
    
    // Filter to relevant nodes (func/class only)
    const graphNodes = NODES.filter(n => n.kind === 'func' || n.kind === 'class');
    
    if (graphNodes.length === 0) {
        ctx.fillStyle = '#666';
        ctx.font = '16px sans-serif';
        ctx.fillText('No functions or classes to display', 20, canvas.height / 2);
        return;
    }
    
    // Group by file for better organization
    const byFile = {};
    graphNodes.forEach(n => {
        const file = n.path || 'unknown';
        if (!byFile[file]) byFile[file] = [];
        byFile[file].push(n);
    });
    
    const files = Object.keys(byFile);
    const nodeMap = {};
    
    // Grid layout: arrange files in columns, nodes in rows within each file
    const cols = Math.min(files.length, 5);
    const colWidth = canvas.width / cols;
    const padding = 40;
    
    files.forEach((file, fileIdx) => {
        const col = fileIdx % cols;
        const row = Math.floor(fileIdx / cols);
        const nodes = byFile[file];
        
        // Draw file label
        ctx.fillStyle = '#999';
        ctx.font = 'bold 12px sans-serif';
        const fileLabel = file.split('/').pop();
        ctx.fillText(fileLabel, col * colWidth + 10, row * 200 + 20);
        
        // Position nodes in grid within this file's area
        const nodesPerRow = Math.ceil(Math.sqrt(nodes.length));
        const nodeSpacing = Math.min(60, (colWidth - padding) / nodesPerRow);
        
        nodes.forEach((node, idx) => {
            const nodeCol = idx % nodesPerRow;
            const nodeRow = Math.floor(idx / nodesPerRow);
            
            nodeMap[node.node_id] = {
                ...node,
                x: col * colWidth + padding + nodeCol * nodeSpacing,
                y: row * 200 + 40 + nodeRow * 40
            };
        });
    });
    
    // Build edge map for only nodes we're displaying
    const edgeSet = new Set();
    EDGES.forEach(edge => {
        if (nodeMap[edge.src] && nodeMap[edge.dst]) {
            edgeSet.add(JSON.stringify([edge.src, edge.dst]));
        }
    });
    
    // Draw edges (only between displayed nodes)
    ctx.strokeStyle = 'rgba(150, 150, 150, 0.3)';
    ctx.lineWidth = 1;
    edgeSet.forEach(edgeStr => {
        const [srcId, dstId] = JSON.parse(edgeStr);
        const src = nodeMap[srcId];
        const dst = nodeMap[dstId];
        if (src && dst && src !== dst) {
            ctx.beginPath();
            ctx.moveTo(src.x, src.y);
            ctx.lineTo(dst.x, dst.y);
            ctx.stroke();
        }
    });
    
    // Draw nodes
    Object.values(nodeMap).forEach(node => {
        // Node circle
        ctx.beginPath();
        const radius = answerNodes.has(node.node_id) ? 10 : 6;
        ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
        
        if (answerNodes.has(node.node_id)) {
            ctx.fillStyle = '#28a745';
            ctx.strokeStyle = '#1e7e34';
            ctx.lineWidth = 2;
        } else if (exploredNodes.has(node.node_id)) {
            ctx.fillStyle = '#ffc107';
            ctx.strokeStyle = '#e0a800';
            ctx.lineWidth = 2;
        } else {
            ctx.fillStyle = '#667eea';
            ctx.strokeStyle = '#5568d3';
            ctx.lineWidth = 1;
        }
        ctx.fill();
        ctx.stroke();
        
        // Label with background (only for answer and explored nodes)
        if (answerNodes.has(node.node_id) || exploredNodes.has(node.node_id)) {
            const label = node.symbol || '';
            if (label && label.length < 20) {
                ctx.font = 'bold 11px sans-serif';
                const metrics = ctx.measureText(label);
                ctx.fillStyle = 'rgba(255,255,255,0.95)';
                ctx.strokeStyle = '#333';
                ctx.lineWidth = 1;
                const boxX = node.x - metrics.width / 2 - 3;
                const boxY = node.y + 12;
                ctx.fillRect(boxX, boxY, metrics.width + 6, 16);
                ctx.strokeRect(boxX, boxY, metrics.width + 6, 16);
                ctx.fillStyle = '#333';
                ctx.fillText(label, node.x - metrics.width / 2, node.y + 24);
            }
        }
    });
    
    // Draw legend
    ctx.font = '12px sans-serif';
    ctx.fillStyle = '#666';
    ctx.fillText(`Showing ${graphNodes.length} functions/classes grouped by file`, 10, canvas.height - 10);
}

// ==================== ARCHITECTURE ====================
function renderArchitecture() {
    const canvas = document.getElementById('arch-canvas');
    canvas.dataset.rendered = 'true';
    const ctx = canvas.getContext('2d');
    canvas.width = canvas.offsetWidth;
    canvas.height = 600;
    
    // Group nodes by package
    const packages = {};
    NODES.forEach(node => {
        if (node.kind === 'pkg') {
            packages[node.symbol] = {
                files: [],
                functions: [],
                classes: []
            };
        }
    });
    
    NODES.forEach(node => {
        const pkg = node.path?.split('/')[0];
        if (pkg && packages[pkg]) {
            if (node.kind === 'file') packages[pkg].files.push(node);
            else if (node.kind === 'func') packages[pkg].functions.push(node);
            else if (node.kind === 'class') packages[pkg].classes.push(node);
        }
    });
    
    // Draw package boxes
    const colors = ['#667eea', '#f093fb', '#4facfe', '#43e97b', '#fa709a'];
    let x = 50, y = 50;
    
    Object.keys(packages).forEach((pkgName, i) => {
        const pkg = packages[pkgName];
        const width = 200;
        const height = 150;
        
        ctx.fillStyle = colors[i % colors.length] + '20';
        ctx.fillRect(x, y, width, height);
        ctx.strokeStyle = colors[i % colors.length];
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, width, height);
        
        ctx.fillStyle = '#333';
        ctx.font = 'bold 14px sans-serif';
        ctx.fillText(pkgName, x + 10, y + 25);
        
        ctx.font = '11px sans-serif';
        ctx.fillStyle = '#666';
        ctx.fillText(`Files: ${pkg.files.length}`, x + 10, y + 50);
        ctx.fillText(`Classes: ${pkg.classes.length}`, x + 10, y + 70);
        ctx.fillText(`Functions: ${pkg.functions.length}`, x + 10, y + 90);
        
        x += 250;
        if (x > canvas.width - 200) {
            x = 50;
            y += 200;
        }
    });
}

// ==================== RESULTS ====================
function renderResults() {
    const container = document.getElementById('results-grid');
    const results = DATA.results || [];
    
    results.forEach(r => {
        const card = document.createElement('div');
        card.className = 'result-card';
        card.innerHTML = `
            <div class="result-symbol">
                <span class="result-kind">${r.kind}</span>
                ${escapeHtml(r.symbol || '(anonymous)')}
            </div>
            <div class="result-path">${escapeHtml(r.path || '')}</div>
            <div style="margin-top:8px">
                <span class="conf-high" style="padding:4px 10px;border-radius:12px;font-size:12px">
                    ${((r.score || 0) * 100).toFixed(0)}% confidence
                </span>
            </div>
            ${r.reasoning ? `<div class="result-reasoning">"${escapeHtml(r.reasoning)}"</div>` : ''}
        `;
        container.appendChild(card);
    });
}

// Initialize
const treeRoot = buildTreeStructure();
if (treeRoot) {
    renderTreeNode(treeRoot, document.getElementById('tree-container'));
}
renderResults();
</script>
</body>
</html>
"""
    )

    with open(os.path.join(tdir, "trace.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return True


def search_llm(
    index_dir: str,
    query: str,
    *,
    top: int = 10,
    budget: int = 50,
    model: str = "gpt-4o-mini",
) -> dict:
    """
    Perform LLM-guided reasoning search through the code tree.

    Args:
        index_dir: Path to index directory
        query: Natural language query
        top: Max results to return
        budget: Max nodes to evaluate (LLM calls)
        model: OpenAI model for reasoning

    Returns:
        Dict with results, trace, and paths to trace files
    """
    nodes = load_nodes(index_dir)
    children = children_index(nodes)

    # Run async LLM search
    result = asyncio.run(
        llm_guided_search(nodes, children, query, model=model, budget=budget, top=top)
    )

    # Save trace and results
    tdir = os.path.join(index_dir, "trace")
    os.makedirs(tdir, exist_ok=True)

    trace_data = {
        "query": query,
        "results": result["results"],
        "trace": result["trace"],
        "stats": result["stats"],
        "mode": "llm",
    }

    with open(os.path.join(tdir, "last_trace.json"), "w", encoding="utf-8") as f:
        json.dump(trace_data, f, ensure_ascii=False, indent=2)

    with open(os.path.join(tdir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(result["results"], f, ensure_ascii=False, indent=2)

    build_trace_html(index_dir)

    return {
        "results": result["results"],
        "trace_path": os.path.join(tdir, "last_trace.json"),
        "html": os.path.join(tdir, "trace.html"),
        "stats": result["stats"],
    }
