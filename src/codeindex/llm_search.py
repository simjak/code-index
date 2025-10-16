"""
LLM-guided tree search for code retrieval.

This module implements reasoning-based retrieval where an LLM evaluates nodes
in the code tree to decide which branches to explore and which nodes answer the query.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from .logger import logger


@dataclass
class NodeRelevance:
    """LLM's assessment of a node's relevance to the query."""

    relevant: bool  # Is this node relevant?
    confidence: float  # 0.0-1.0 confidence score
    reasoning: str  # Why is it relevant/irrelevant?
    is_answer: bool  # Does this node directly answer the query?


@dataclass
class ChildRanking:
    """LLM's ranking of children to explore."""

    child_id: str
    relevance_score: float  # 0.0-1.0
    reasoning: str


async def evaluate_node_relevance(
    node: dict,
    query: str,
    *,
    model: str = "gpt-4o-mini",
    client: Optional[AsyncOpenAI] = None,
    reasoning_effort: str = "low",
) -> NodeRelevance:
    """
    Ask LLM: Is this node relevant to the query?

    Args:
        node: Node dictionary with summary, symbol, path, kind
        query: User's natural language query
        model: OpenAI model to use
        client: Optional pre-initialized client

    Returns:
        NodeRelevance with LLM's assessment
    """
    should_close = False
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set")
        client = AsyncOpenAI(api_key=api_key, timeout=15.0)
        should_close = True

    try:
        # Build context about the node
        node_context = f"""
Node Type: {node["kind"]}
Symbol: {node.get("symbol", "(anonymous)")}
Path: {node.get("path", "unknown")}
Summary: {node.get("summary", "No summary available")}
"""

        system_prompt = """You are a code navigation expert. Think first, then respond exactly in the requested JSON format. Stay factual—never invent behavior or files that are not in the input.

Respond in JSON format:
{
    "relevant": true/false,
    "confidence": 0.0-1.0,
    "reasoning": "brief explanation",
    "is_answer": true/false
}

Set "is_answer" to TRUE if this node IMPLEMENTS or DEFINES what the query asks about:
- Functions that implement features mentioned in the query
- Classes that define components asked about
- Constants/configs that define values asked about

Set "is_answer" to FALSE only for container nodes (packages, files) or nodes that just reference/call other code."""

        user_prompt = f"""Query: "{query}"

Code Node:
{node_context}

Is this node relevant? Does it answer the query?"""

        params = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 200,
        }
        if "gpt-5" in model.lower():
            params["reasoning_effort"] = reasoning_effort
            params["verbosity"] = "low"
        else:
            params["temperature"] = 0.2

        resp = await client.chat.completions.create(**params)

        content = resp.choices[0].message.content
        if not content:
            logger.warning("Empty response from LLM for node %s", node.get("node_id"))
            return NodeRelevance(False, 0.0, "No response", False)

        result = json.loads(content)
        return NodeRelevance(
            relevant=result.get("relevant", False),
            confidence=result.get("confidence", 0.0),
            reasoning=result.get("reasoning", ""),
            is_answer=result.get("is_answer", False),
        )

    except Exception as e:
        logger.error("LLM evaluation failed for node %s: %s", node.get("node_id"), e)
        return NodeRelevance(False, 0.0, f"Error: {e}", False)

    finally:
        if should_close:
            await client.close()


async def rank_children(
    children: list[dict],
    query: str,
    parent_context: str = "",
    *,
    model: str = "gpt-4o-mini",
    client: Optional[AsyncOpenAI] = None,
    top_k: int = 5,
    reasoning_effort: str = "low",
) -> list[ChildRanking]:
    """
    Ask LLM: Which children should we explore?

    Args:
        children: List of child node dictionaries
        query: User's natural language query
        parent_context: Context about the parent node
        model: OpenAI model to use
        client: Optional pre-initialized client
        top_k: Max children to rank
        reasoning_effort: GPT-5 reasoning effort level when applicable

    Returns:
        List of ChildRanking sorted by relevance
    """
    if not children:
        return []

    should_close = False
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set")
        client = AsyncOpenAI(api_key=api_key, timeout=20.0)
        should_close = True

    try:
        # Build context about children
        children_context = []
        for i, child in enumerate(children[:20], 1):  # Limit to 20 for context length
            summary = child.get("summary") or "No summary"
            children_context.append(
                f"{i}. {child['kind']} '{child.get('symbol', '?')}' - {summary[:100]}"
            )

        system_prompt = """You are a code navigation expert. Given a query and a list of code nodes, rank which ones are MOST LIKELY to contain relevant information.

Respond in JSON format:
{
    "rankings": [
        {"index": 1, "score": 0.0-1.0, "reasoning": "why relevant"},
        ...
    ]
}

Be EXPLORATORY - include any children with score > 0.2. Even if you're not certain, include nodes that might lead to the answer. Rank up to 5 children."""

        user_prompt = f"""Query: "{query}"

{parent_context}

Children to evaluate:
{chr(10).join(children_context)}

Which children should we explore?"""

        params = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 400,
        }
        if "gpt-5" in model.lower():
            params["reasoning_effort"] = reasoning_effort
            params["verbosity"] = "low"
        else:
            params["temperature"] = 0.2

        resp = await client.chat.completions.create(**params)

        content = resp.choices[0].message.content
        if not content:
            logger.warning("Empty response from LLM for child ranking")
            return []

        result = json.loads(content)
        rankings = []

        for ranking in result.get("rankings", []):
            idx = ranking.get("index", 0) - 1
            if 0 <= idx < len(children):
                rankings.append(
                    ChildRanking(
                        child_id=children[idx]["node_id"],
                        relevance_score=ranking.get("score", 0.0),
                        reasoning=ranking.get("reasoning", ""),
                    )
                )

        return sorted(rankings, key=lambda r: r.relevance_score, reverse=True)[:top_k]

    except Exception as e:
        logger.error("LLM child ranking failed: %s", e)
        return []

    finally:
        if should_close:
            await client.close()


async def llm_guided_search(
    nodes: dict,
    children_index: dict,
    query: str,
    *,
    model: str = "gpt-4o-mini",
    budget: int = 50,
    top: int = 10,
    reasoning_effort: str = "low",
) -> dict:
    """
    Perform LLM-guided tree search through the code.

    Args:
        nodes: Dict of all nodes {node_id: node_dict}
        children_index: Dict mapping node_id to list of child node_ids
        query: User's natural language query
        model: OpenAI model to use for reasoning
        budget: Max number of nodes to evaluate
        top: Max number of answer nodes to return
        reasoning_effort: GPT-5 reasoning effort when applicable

    Returns:
        Dict with results and trace of LLM reasoning
    """
    client = AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=30.0,
        max_retries=2,
    )

    # Find root node
    root = next((nid for nid, n in nodes.items() if n["parent_id"] is None), None)
    if not root:
        raise ValueError("No root node found")

    frontier = [(root, 1.0)]  # (node_id, score)
    visited = set()
    answers = []  # (node_id, score, reasoning)
    trace = []
    steps = 0

    logger.info("Starting LLM-guided search: query='%s' budget=%d", query, budget)

    try:
        while frontier and steps < budget:
            steps += 1

            # Pop highest scored node
            frontier.sort(key=lambda x: x[1], reverse=True)
            node_id, parent_score = frontier.pop(0)

            if node_id in visited:
                continue

            visited.add(node_id)
            node = nodes[node_id]

            # Ask LLM: Is this node relevant?
            relevance = await evaluate_node_relevance(
                node,
                query,
                model=model,
                client=client,
                reasoning_effort=reasoning_effort,
            )

            trace.append(
                {
                    "step": steps,
                    "event": "evaluate",
                    "node_id": node_id,
                    "symbol": node.get("symbol", "?"),
                    "path": node.get("path", "?"),
                    "relevant": relevance.relevant,
                    "confidence": relevance.confidence,
                    "reasoning": relevance.reasoning,
                    "is_answer": relevance.is_answer,
                }
            )

            logger.debug(
                "Step %d: Evaluated %s - relevant=%s conf=%.2f answer=%s",
                steps,
                node.get("symbol", "?"),
                relevance.relevant,
                relevance.confidence,
                relevance.is_answer,
            )

            # If this is an answer node, add it
            if relevance.is_answer and node["kind"] in (
                "func",
                "block",
                "const",
                "class",
            ):
                answers.append((node_id, relevance.confidence, relevance.reasoning))
                trace.append(
                    {
                        "step": steps,
                        "event": "answer",
                        "node_id": node_id,
                        "score": relevance.confidence,
                        "reasoning": relevance.reasoning,
                    }
                )
                continue

            # ALWAYS explore container nodes (repo/pkg/file/class) - they're just organizational
            # For other nodes, only explore if relevant
            is_container = node["kind"] in ("repo", "pkg", "file", "class")
            should_explore = (
                is_container  # Always explore containers
                or (relevance.relevant and relevance.confidence > 0.3)
                or relevance.confidence > 0.15
            )

            if should_explore:
                child_ids = children_index.get(node_id, [])
                logger.debug(
                    "Node %s has %d children, exploring...",
                    node.get("symbol", "?"),
                    len(child_ids),
                )
                if child_ids:
                    child_nodes = [nodes[cid] for cid in child_ids if cid in nodes]

                    # Ask LLM to rank children
                    parent_summary = node.get("summary") or ""
                    rankings = await rank_children(
                        child_nodes,
                        query,
                        parent_context=f"Parent: {node.get('symbol', '?')} - {parent_summary[:100]}",
                        model=model,
                        client=client,
                        reasoning_effort=reasoning_effort,
                    )

                    logger.debug(
                        "LLM ranked %d/%d children for exploration",
                        len(rankings),
                        len(child_nodes),
                    )

                    for ranking in rankings:
                        frontier.append((ranking.child_id, ranking.relevance_score))
                        trace.append(
                            {
                                "step": steps,
                                "event": "expand",
                                "node_id": ranking.child_id,
                                "score": ranking.relevance_score,
                                "reasoning": ranking.reasoning,
                            }
                        )

        # Sort answers by confidence
        answers.sort(key=lambda x: x[1], reverse=True)

        results = [
            {
                "node_id": nid,
                "score": score,
                "reasoning": reasoning,
                "path": nodes[nid]["path"],
                "symbol": nodes[nid].get("symbol"),
                "kind": nodes[nid]["kind"],
                "summary": nodes[nid].get("summary", ""),
            }
            for nid, score, reasoning in answers[:top]
        ]

        logger.info(
            "LLM search complete: %d steps, %d nodes visited, %d answers",
            steps,
            len(visited),
            len(results),
        )

        return {
            "query": query,
            "results": results,
            "trace": trace,
            "stats": {
                "steps": steps,
                "nodes_visited": len(visited),
                "answers_found": len(answers),
                "budget": budget,
            },
        }

    finally:
        await client.close()
