from __future__ import annotations
import argparse
import json
import os
import webbrowser
from .indexer import build
from .searcher import search, build_trace_html
from .logger import logger


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
    )
    logger.info("Index written to: %s", out)


def cmd_search(args):
    res = search(
        args.index, args.query, top=args.top, budget=args.budget, gate=args.gate
    )
    print(json.dumps(res["results"], ensure_ascii=False, indent=2))
    print(f"Trace JSON: {res['trace_path']}")
    print(f"Trace HTML: {res['html']}")


def cmd_trace(args):
    build_trace_html(args.index)
    html = os.path.join(args.index, "trace", "trace.html")
    print(f"Trace HTML: {html}")
    if args.open_html and os.path.exists(html):
        webbrowser.open("file://" + os.path.abspath(html))


def main():
    p = argparse.ArgumentParser(
        prog="codeindex",
        description="JSON-only multi-resolution index + BM25 + TraceView",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    p_b = sub.add_parser("build", help="Build JSON index")
    p_b.add_argument("repo")
    p_b.add_argument("--out", default="./index")
    p_b.add_argument(
        "--summarizer", default="off", choices=["off", "gpt-5-mini", "gpt-5-nano"]
    )
    p_b.add_argument(
        "--min-loc", type=int, default=20, help="Min LOC to summarize nodes"
    )
    p_b.set_defaults(func=cmd_build)
    p_s = sub.add_parser("search", help="Search with BM25 + hierarchical trace")
    p_s.add_argument("--index", default="./index")
    p_s.add_argument("--top", type=int, default=10)
    p_s.add_argument("--budget", type=int, default=120, help="Max expand steps")
    p_s.add_argument("--gate", default="off", choices=["off"])
    p_s.add_argument("query")
    p_s.set_defaults(func=cmd_search)
    p_t = sub.add_parser("trace", help="Build/open HTML trace viewer")
    p_t.add_argument("--index", default="./index")
    p_t.add_argument("--open-html", action="store_true")
    p_t.set_defaults(func=cmd_trace)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
