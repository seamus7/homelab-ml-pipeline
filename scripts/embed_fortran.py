#!/usr/bin/env python3
"""
Read summarized Fortran chunks (JSON from summarize_fortran.py) and annotate
each with a vector embedding of its 'summary' field using nomic-embed-text
via Ollama.

Usage:
    ... | python3 embed_fortran.py
    python3 embed_fortran.py summarized.json
    python3 embed_fortran.py --ollama-url http://localhost:11434 summarized.json
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


_DEFAULT_OLLAMA_URL = "http://ollama:11434"
_EMBED_MODEL = "nomic-embed-text"


def _check_model(base_url: str) -> None:
    """Exit with a clear message if nomic-embed-text is not available."""
    url = base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.URLError as exc:
        print(f"error: cannot reach Ollama at {base_url}: {exc}", file=sys.stderr)
        sys.exit(1)

    models = [m["name"] for m in body.get("models", [])]
    # model names may include a tag suffix like "nomic-embed-text:latest"
    if not any(_EMBED_MODEL in m for m in models):
        print(
            f"{_EMBED_MODEL} not found. Run: ollama pull {_EMBED_MODEL}",
            file=sys.stderr,
        )
        sys.exit(1)


def _embed(base_url: str, text: str) -> list[float]:
    # Ollama ≥0.4 uses /api/embed with "input" key; response is {"embeddings": [[...]]}.
    url = base_url.rstrip("/") + "/api/embed"
    payload = json.dumps({"model": _EMBED_MODEL, "input": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
            return body["embeddings"][0]
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama embeddings request failed: {exc}") from exc
    except (KeyError, IndexError):
        raise RuntimeError("Ollama response missing 'embeddings' field")


def embed_chunks(chunks: list[dict], ollama_url: str) -> list[dict]:
    total = len(chunks)
    results = []
    for i, chunk in enumerate(chunks, start=1):
        name = chunk["name"]
        print(f"Embedding {i}/{total}: {name}...", file=sys.stderr, flush=True)
        summary = chunk.get("summary", "")
        if not summary:
            print(
                f"  warning: {name} has no summary field — embedding empty string",
                file=sys.stderr,
            )
        embedding = _embed(ollama_url, summary)
        results.append({**chunk, "embedding": embedding})
    return results


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Annotate summarized Fortran chunks with nomic-embed-text embeddings."
    )
    ap.add_argument(
        "input",
        nargs="?",
        help="JSON file from summarize_fortran.py (omit to read from stdin)",
    )
    ap.add_argument(
        "--ollama-url",
        default=_DEFAULT_OLLAMA_URL,
        metavar="URL",
        help=f"Ollama base URL (default: {_DEFAULT_OLLAMA_URL})",
    )
    args = ap.parse_args()

    _check_model(args.ollama_url)

    if args.input:
        try:
            with open(args.input) as fh:
                chunks = json.load(fh)
        except OSError as exc:
            print(f"error: cannot read {args.input}: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            chunks = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON on stdin: {exc}", file=sys.stderr)
            sys.exit(1)

    if not isinstance(chunks, list):
        print("error: expected a JSON array of chunks", file=sys.stderr)
        sys.exit(1)

    try:
        annotated = embed_chunks(chunks, args.ollama_url)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    json.dump(annotated, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
