from __future__ import annotations

import argparse
import json
import os
import webbrowser
from pathlib import Path

from .arch import ArchConfig, generate_architecture
from .indexer import build
from .logger import logger
from .searcher import build_trace_html, search, search_llm


def cmd_build(args):
    logger.info(
        "Starting build; repo=%s out=%s summarizer=%s min_loc=%s",
        args.repo,
        args.out,
        args.summarizer,
        args.min_loc,
    )
    out = build(
        args.repo,
        args.out,
        summarizer=args.summarizer,
        min_loc_for_summary=args.min_loc,
        summary_scope=args.summary_scope,
    )
    logger.info("Index written to: %s", out)


def cmd_search(args):
    if args.mode == "llm":
        logger.info("Using LLM-guided search (reasoning mode)")
        res = search_llm(
            args.index,
            args.query,
            top=args.top,
            budget=args.budget,
            model=args.llm_model,
        )
    else:
        logger.info("Using BM25 + hierarchical search (keyword mode)")
        res = search(
            args.index, args.query, top=args.top, budget=args.budget, gate=args.gate
        )
    print(json.dumps(res["results"], ensure_ascii=False, indent=2))
    print(f"Trace JSON: {res['trace_path']}")
    print(f"Trace HTML: {res['html']}")


def cmd_trace(args):
    ok = build_trace_html(args.index)
    html = os.path.join(args.index, "trace", "trace.html")
    if ok and os.path.exists(html):
        print(f"Trace HTML: {html}")
        if args.open_html:
            webbrowser.open("file://" + os.path.abspath(html))
    else:
        print("No trace data found. Run a search first, e.g.:")
        print('  uv run codeindex search --index ./index "your query"')


def cmd_arch(args):
    verbosity = None if args.llm_verbosity == "off" else args.llm_verbosity
    config = ArchConfig(
        index_dir=Path(args.index),
        out_dir=Path(args.out),
        llm_model=args.llm_model,
        max_tokens=args.llm_max_tokens,
        temperature=args.llm_temperature,
        reasoning_effort=args.llm_reasoning_effort,
        verbosity=verbosity,
        stub=args.llm_stub,
        diagram_mode=args.diagram_mode,
        diagram_coverage=args.diagram_coverage,
        diagram_max_redrafts=args.diagram_redrafts,
    )
    generate_architecture(config)


def main():
    p = argparse.ArgumentParser(
        prog="codeindex",
        description="LLM-powered reasoning search over code with hierarchical indexing",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    p_b = sub.add_parser("build", help="Build JSON index")
    p_b.add_argument("repo")
    p_b.add_argument("--out", default="./index")
    p_b.add_argument(
        "--summarizer",
        default="off",
        help=(
            "Model to use for code summarization (default: off). "
            "Recommended: gpt-4o-mini for speed/cost, gpt-4o for quality. "
            "Set OPENAI_API_KEY env variable to enable."
        ),
    )
    p_b.add_argument(
        "--min-loc", type=int, default=20, help="Min LOC to summarize nodes"
    )
    p_b.add_argument(
        "--summary-scope",
        default="structured",
        choices=["structured", "files", "none"],
        help=(
            "Scope of LLM summaries: 'structured' (default) summarizes functions/classes, "
            "'files' summarizes only whole files, 'none' disables summaries."
        ),
    )
    p_b.set_defaults(func=cmd_build)
    p_s = sub.add_parser("search", help="Search with BM25 or LLM reasoning")
    p_s.add_argument("--index", default="./index")
    p_s.add_argument("--top", type=int, default=10)
    p_s.add_argument("--budget", type=int, default=120, help="Max expand steps")
    p_s.add_argument("--gate", default="off", choices=["off"])
    p_s.add_argument(
        "--mode",
        default="llm",
        choices=["llm", "bm25"],
        help=(
            "Search mode: 'llm' for reasoning-based (default, requires OPENAI_API_KEY), "
            "'bm25' for fast keyword-based search (no API key needed)"
        ),
    )
    p_s.add_argument(
        "--llm-model",
        default="gpt-4o-mini",
        help="LLM model for reasoning mode (default: gpt-4o-mini)",
    )
    p_s.add_argument("query")
    p_s.set_defaults(func=cmd_search)
    p_t = sub.add_parser("trace", help="Build/open HTML trace viewer")
    p_t.add_argument("--index", default="./index")
    p_t.add_argument("--open-html", action="store_true")
    p_t.set_defaults(func=cmd_trace)
    p_a = sub.add_parser("arch", help="Generate architecture and structure docs")
    p_a.add_argument("--index", default="./index", help="Path to existing index dir")
    p_a.add_argument("--out", default="./arch", help="Output directory for artifacts")
    p_a.add_argument(
        "--llm-model",
        default="gpt-5-mini",
        help="LLM model to use for architecture documentation (default: gpt-5-mini)",
    )
    p_a.add_argument(
        "--llm-max-tokens",
        type=int,
        default=4000,
        help="Maximum completion tokens for LLM outputs (default: 4000)",
    )
    p_a.add_argument(
        "--llm-reasoning-effort",
        choices=["minimal", "low", "medium", "high"],
        default="medium",
        help="Reasoning effort for GPT-5 family models (default: medium)",
    )
    p_a.add_argument(
        "--llm-verbosity",
        choices=["low", "medium", "high", "off"],
        default="medium",
        help="Verbosity preference for GPT-5 models (default: medium, use 'off' to skip)",
    )
    p_a.add_argument(
        "--llm-stub",
        action="store_true",
        help="Use deterministic stub responses instead of calling the OpenAI API",
    )
    p_a.add_argument(
        "--diagram-mode",
        choices=["llm", "hybrid", "deterministic"],
        default="llm",
        help=(
            "Diagram generation mode: 'llm' for full LLM flow (default), 'hybrid' to fall back to deterministic on failure, "
            "or 'deterministic' to bypass LLM entirely."
        ),
    )
    p_a.add_argument(
        "--diagram-coverage",
        type=float,
        default=0.95,
        help="Required component coverage threshold for diagrams (default: 0.95)",
    )
    p_a.add_argument(
        "--diagram-redrafts",
        type=int,
        default=2,
        help="Maximum LLM redraft attempts before failing or falling back (default: 2)",
    )
    p_a.set_defaults(func=cmd_arch)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
