#!/usr/bin/env python3
"""
Full pipeline: parse Fortran → summarize → embed → upsert to Qdrant.

Usage:
    python3 index_fortran.py ../src/
    python3 index_fortran.py file.f90 file2.f --reset
    python3 index_fortran.py ../src/ \\
        --ollama-url http://localhost:11434 \\
        --qdrant-url http://qdrant:6333 \\
        --collection fortran_subroutines
"""

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Import parse_fortran from the same directory
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))
import parse_fortran  # noqa: E402  (local import after sys.path manipulation)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_OLLAMA_URL = "http://ollama:11434"
_DEFAULT_QDRANT_URL = "http://qdrant:6333"
_DEFAULT_COLLECTION = "fortran_subroutines"

_GEN_MODEL = "qwen3-coder-next"
_EMBED_MODEL = "nomic-embed-text"
_EMBED_DIM = 768
_UPSERT_BATCH = 10

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http(method: str, url: str, body: dict | None = None, timeout: int = 120) -> dict:
    """Minimal HTTP helper. Returns parsed JSON body. Raises RuntimeError on failure."""
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


def _point_id(source_file: str, name: str) -> int:
    """Stable positive integer ID — SHA-256 of 'source_file\x00name', mod 2^53."""
    key = f"{source_file}\x00{name}".encode()
    digest = hashlib.sha256(key).digest()
    raw = int.from_bytes(digest[:8], "big")
    return raw % (2 ** 53)


# ---------------------------------------------------------------------------
# Stage 1 – parse  (delegates entirely to parse_fortran module)
# ---------------------------------------------------------------------------


def stage_parse(paths: list[str]) -> list[dict]:
    print("Parsing...", file=sys.stderr, flush=True)
    files = parse_fortran._collect_files(paths)
    if not files:
        print("error: no Fortran files found", file=sys.stderr)
        sys.exit(1)
    base_dir = parse_fortran._common_base(files)
    chunks: list[dict] = []
    for f in files:
        chunks.extend(parse_fortran.parse_file(f, base_dir))
    return chunks


# ---------------------------------------------------------------------------
# Stage 2 – summarize  (inline from summarize_fortran.py)
# ---------------------------------------------------------------------------


def _build_prompt(chunk: dict) -> str:
    return (
        f"You are analyzing a Fortran subroutine from a geodesy codebase.\n"
        f"Subroutine name: {chunk['name']}\n"
        f"Type: {chunk['type']}\n"
        f"File: {chunk['source_file']}, lines {chunk['line_start']}-{chunk['line_end']}\n"
        f"Calls: {chunk.get('calls', [])} (empty list means no calls to other routines)\n"
        f"Called by: {chunk.get('called_by', [])} (pass empty list for now — indexer will populate later)\n"
        f"\n"
        f"Source code:\n"
        f"{chunk['raw_code']}\n"
        f"\n"
        f"Write a plain-English description of what this subroutine does.\n"
        f"Be specific about: what inputs it takes, what it computes or transforms,\n"
        f"what outputs or side effects it produces. If variable names are opaque,\n"
        f"reason from the operations and call patterns. 3-5 sentences maximum.\n"
        f"Return only the description, no preamble."
    )


def _generate(ollama_url: str, prompt: str) -> str:
    url = ollama_url.rstrip("/") + "/api/generate"
    body = {"model": _GEN_MODEL, "prompt": prompt, "stream": False}
    result = _http("POST", url, body, timeout=120)
    return result.get("response", "").strip()


def stage_summarize(chunks: list[dict], ollama_url: str) -> list[dict]:
    total = len(chunks)
    out = []
    for i, chunk in enumerate(chunks, start=1):
        print(f"Summarizing {i}/{total}: {chunk['name']}...", file=sys.stderr, flush=True)
        summary = _generate(ollama_url, _build_prompt(chunk))
        out.append({**chunk, "summary": summary})
    return out


# ---------------------------------------------------------------------------
# Stage 3 – embed  (inline from embed_fortran.py)
# ---------------------------------------------------------------------------


def _check_embed_model(ollama_url: str) -> None:
    url = ollama_url.rstrip("/") + "/api/tags"
    try:
        body = _http("GET", url, timeout=10)
    except RuntimeError as exc:
        print(f"error: cannot reach Ollama: {exc}", file=sys.stderr)
        sys.exit(1)
    names = [m["name"] for m in body.get("models", [])]
    if not any(_EMBED_MODEL in n for n in names):
        print(
            f"error: {_EMBED_MODEL} not found. Run: ollama pull {_EMBED_MODEL}",
            file=sys.stderr,
        )
        sys.exit(1)


def _embed(ollama_url: str, text: str) -> list[float]:
    # Ollama ≥0.4: /api/embed, key "input", response {"embeddings": [[...]]}
    url = ollama_url.rstrip("/") + "/api/embed"
    body = {"model": _EMBED_MODEL, "input": text}
    result = _http("POST", url, body, timeout=60)
    try:
        return result["embeddings"][0]
    except (KeyError, IndexError):
        raise RuntimeError("Ollama embed response missing 'embeddings' field")


def stage_embed(chunks: list[dict], ollama_url: str) -> list[dict]:
    total = len(chunks)
    out = []
    for i, chunk in enumerate(chunks, start=1):
        print(f"Embedding {i}/{total}: {chunk['name']}...", file=sys.stderr, flush=True)
        summary = chunk.get("summary", "")
        if not summary:
            print(
                f"  warning: {chunk['name']} has no summary — embedding empty string",
                file=sys.stderr,
            )
        embedding = _embed(ollama_url, summary)
        out.append({**chunk, "embedding": embedding})
    return out


# ---------------------------------------------------------------------------
# Stage 4 – upsert to Qdrant
# ---------------------------------------------------------------------------


def _ensure_collection(qdrant_url: str, collection: str, reset: bool) -> None:
    col_url = qdrant_url.rstrip("/") + f"/collections/{collection}"

    if reset:
        try:
            _http("DELETE", col_url, timeout=30)
            print(f"  Deleted existing collection '{collection}'.", file=sys.stderr, flush=True)
        except RuntimeError:
            pass  # didn't exist — fine

    create_body = {"vectors": {"size": _EMBED_DIM, "distance": "Cosine"}}
    try:
        _http("PUT", col_url, create_body, timeout=30)
    except RuntimeError as exc:
        msg = str(exc)
        if "HTTP 400" in msg and not reset:
            # Collection already exists and we weren't asked to reset — fine.
            pass
        else:
            raise


def _upsert_batch(qdrant_url: str, collection: str, points: list[dict]) -> None:
    url = qdrant_url.rstrip("/") + f"/collections/{collection}/points"
    _http("PUT", url, {"points": points}, timeout=60)


def stage_upsert(chunks: list[dict], qdrant_url: str, collection: str) -> None:
    points = []
    for chunk in chunks:
        pid = _point_id(chunk["source_file"], chunk["name"])
        points.append({
            "id": pid,
            "vector": chunk["embedding"],
            "payload": {
                "name":        chunk["name"],
                "type":        chunk["type"],
                "source_file": chunk["source_file"],
                "line_start":  chunk["line_start"],
                "line_end":    chunk["line_end"],
                "summary":     chunk.get("summary", ""),
                "raw_code":    chunk["raw_code"],
                "calls":       chunk.get("calls", []),
            },
        })

    total_batches = (len(points) + _UPSERT_BATCH - 1) // _UPSERT_BATCH
    for b, start in enumerate(range(0, len(points), _UPSERT_BATCH), start=1):
        batch = points[start : start + _UPSERT_BATCH]
        print(f"Upserting batch {b}/{total_batches}...", file=sys.stderr, flush=True)
        _upsert_batch(qdrant_url, collection, batch)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse, summarize, embed, and index Fortran source into Qdrant."
    )
    ap.add_argument(
        "paths",
        nargs="+",
        help="Fortran source files (.f90 / .f / .for) or directories to scan",
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
        "--reset",
        action="store_true",
        help="Delete and recreate the Qdrant collection before indexing",
    )
    args = ap.parse_args()

    # Pre-flight: verify embed model is available before spending time on parsing/summarizing
    _check_embed_model(args.ollama_url)

    # Ensure collection exists (or reset it)
    try:
        _ensure_collection(args.qdrant_url, args.collection, args.reset)
    except RuntimeError as exc:
        print(f"error: Qdrant collection setup failed: {exc}", file=sys.stderr)
        sys.exit(1)

    chunks = stage_parse(args.paths)
    chunks = stage_summarize(chunks, args.ollama_url)
    chunks = stage_embed(chunks, args.ollama_url)

    try:
        stage_upsert(chunks, args.qdrant_url, args.collection)
    except RuntimeError as exc:
        print(f"error: upsert failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Done. {len(chunks)} chunks indexed.", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
