#!/usr/bin/env python3
"""
Embed a plain-English query and search the Fortran subroutine index in Qdrant.

Usage:
    python3 query_fortran.py find routines that handle gravity calculations
    python3 query_fortran.py --top 3 coordinate transformation
    python3 query_fortran.py --qdrant-url http://localhost:6333 read or write files
"""

import argparse
import json
import shutil
import sys
import textwrap
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_OLLAMA_URL = "http://ollama:11434"
_DEFAULT_QDRANT_URL = "http://qdrant:6333"
_DEFAULT_COLLECTION = "fortran_subroutines"
_DEFAULT_TOP = 5

_EMBED_MODEL = "nomic-embed-text"
_CODE_MAX_LINES = 10

# ---------------------------------------------------------------------------
# HTTP helper  (same pattern as index_fortran.py)
# ---------------------------------------------------------------------------


def _http(method: str, url: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {method} {url}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection error {method} {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Embed  (same pattern as index_fortran.py)
# ---------------------------------------------------------------------------


def _embed(ollama_url: str, text: str) -> list[float]:
    url = ollama_url.rstrip("/") + "/api/embed"
    result = _http("POST", url, {"model": _EMBED_MODEL, "input": text}, timeout=60)
    try:
        return result["embeddings"][0]
    except (KeyError, IndexError):
        raise RuntimeError("Ollama embed response missing 'embeddings' field")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search(qdrant_url: str, collection: str, vector: list[float], top: int) -> list[dict]:
    url = qdrant_url.rstrip("/") + f"/collections/{collection}/points/search"
    body = {"vector": vector, "limit": top, "with_payload": True}
    result = _http("POST", url, body, timeout=30)
    return result.get("result", [])


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _box_line(content: str, width: int, pad: int = 1) -> str:
    """Return content padded to fill the interior of a box of `width` total chars."""
    interior = width - 2  # subtract left and right │
    inner = " " * pad + content
    return "│" + inner.ljust(interior) + "│"


def _wrap_lines(text: str, width: int, pad: int = 1) -> list[str]:
    """Word-wrap text to fit inside the box interior."""
    interior = width - 2 - pad
    wrapped = textwrap.wrap(text, width=interior) or [""]
    return [_box_line(line, width, pad) for line in wrapped]


def display_results(hits: list[dict], query: str, width: int) -> None:
    if not hits:
        print("No results found.")
        return

    sep_top    = "┌" + "─" * (width - 2) + "┐"
    sep_mid    = "├" + "─" * (width - 2) + "┤"
    sep_bot    = "└" + "─" * (width - 2) + "┘"
    blank_line = _box_line("", width)

    for i, hit in enumerate(hits, start=1):
        score = hit.get("score", 0.0)
        p = hit.get("payload", {})
        name        = p.get("name", "?")
        kind        = p.get("type", "?")
        source_file = p.get("source_file", "?")
        line_start  = p.get("line_start", "?")
        line_end    = p.get("line_end", "?")
        summary     = p.get("summary", "")
        raw_code    = p.get("raw_code", "")

        header_info = f"Result {i} — score: {score:.3f}"
        meta_info   = f"{name}  ({kind})  {source_file}  lines {line_start}-{line_end}"

        # ── top border + header ──────────────────────────────────────────────
        print(sep_top)
        print(_box_line(header_info, width))
        print(_box_line(meta_info, width))
        print(sep_mid)

        # ── summary ─────────────────────────────────────────────────────────
        print(_box_line("SUMMARY", width))
        print(blank_line)
        for line in _wrap_lines(summary, width):
            print(line)

        # ── code ─────────────────────────────────────────────────────────────
        print(blank_line)
        print(_box_line("CODE", width))
        print(blank_line)

        code_lines = raw_code.splitlines()
        shown = code_lines[:_CODE_MAX_LINES]
        remaining = len(code_lines) - len(shown)

        for cl in shown:
            # Truncate lines that are wider than the box interior
            interior = width - 4  # 1 pad + 1 border each side
            display = cl[:interior]
            print(_box_line(display, width))

        if remaining > 0:
            print(_box_line(f"... ({remaining} more lines)", width))

        print(sep_bot)

        if i < len(hits):
            print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Search the Fortran subroutine index with a plain-English query.",
        # Allow multi-word query without quotes by consuming all positional args
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "query",
        nargs="+",
        help="Plain-English search string (no quotes needed)",
    )
    ap.add_argument(
        "--ollama-url",
        default=_DEFAULT_OLLAMA_URL,
        metavar="URL",
        help=f"Ollama base URL (default: {_DEFAULT_OLLAMA_URL})",
    )
    ap.add_argument(
        "--qdrant-url",
        default=_DEFAULT_QDRANT_URL,
        metavar="URL",
        help=f"Qdrant base URL (default: {_DEFAULT_QDRANT_URL})",
    )
    ap.add_argument(
        "--collection",
        default=_DEFAULT_COLLECTION,
        metavar="NAME",
        help=f"Qdrant collection name (default: {_DEFAULT_COLLECTION})",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=_DEFAULT_TOP,
        metavar="N",
        help=f"Number of results to return (default: {_DEFAULT_TOP})",
    )
    args = ap.parse_args()

    query_str = " ".join(args.query)
    width = min(shutil.get_terminal_size().columns, 100)

    try:
        vector = _embed(args.ollama_url, query_str)
    except RuntimeError as exc:
        print(f"error: embedding query failed: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        hits = search(args.qdrant_url, args.collection, vector, args.top)
    except RuntimeError as exc:
        print(f"error: Qdrant search failed: {exc}", file=sys.stderr)
        sys.exit(1)

    display_results(hits, query_str, width)


if __name__ == "__main__":
    main()
