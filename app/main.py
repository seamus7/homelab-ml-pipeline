"""
FastAPI service exposing Fortran codebase search and indexing.

Configuration (environment variables):
  OLLAMA_URL    default http://ollama:11434
  QDRANT_URL    default http://qdrant:6333
  COLLECTION    default fortran_subroutines
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Import parse_fortran from sibling scripts/ directory
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import parse_fortran  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.environ.get("COLLECTION", "fortran_subroutines")

_GEN_MODEL   = "qwen3-coder-next"
_EMBED_MODEL = "nomic-embed-text"
_EMBED_DIM   = 768
_UPSERT_BATCH = 10

# ---------------------------------------------------------------------------
# HTTP helper  (copied from index_fortran.py)
# ---------------------------------------------------------------------------


def _http(method: str, url: str, body: dict | None = None, timeout: int = 60) -> dict:
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


# ---------------------------------------------------------------------------
# Embed  (copied from index_fortran.py)
# ---------------------------------------------------------------------------


def _embed(ollama_url: str, text: str) -> list[float]:
    # Ollama ≥0.4: /api/embed, key "input", response {"embeddings": [[...]]}
    url = ollama_url.rstrip("/") + "/api/embed"
    result = _http("POST", url, {"model": _EMBED_MODEL, "input": text}, timeout=60)
    try:
        return result["embeddings"][0]
    except (KeyError, IndexError):
        raise RuntimeError("Ollama embed response missing 'embeddings' field")


# ---------------------------------------------------------------------------
# Pipeline helpers  (inlined from index_fortran.py)
# ---------------------------------------------------------------------------


def _point_id(source_file: str, name: str) -> int:
    key = f"{source_file}\x00{name}".encode()
    digest = hashlib.sha256(key).digest()
    return int.from_bytes(digest[:8], "big") % (2 ** 53)


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
    result = _http("POST", url, {"model": _GEN_MODEL, "prompt": prompt, "stream": False}, timeout=120)
    return result.get("response", "").strip()


def _ensure_collection(qdrant_url: str, collection: str, reset: bool) -> None:
    col_url = qdrant_url.rstrip("/") + f"/collections/{collection}"
    if reset:
        try:
            _http("DELETE", col_url, timeout=30)
        except RuntimeError:
            pass
    try:
        _http("PUT", col_url, {"vectors": {"size": _EMBED_DIM, "distance": "Cosine"}}, timeout=30)
    except RuntimeError as exc:
        if "HTTP 400" in str(exc) and not reset:
            pass  # already exists
        else:
            raise


def _upsert_batch(qdrant_url: str, collection: str, points: list[dict]) -> None:
    url = qdrant_url.rstrip("/") + f"/collections/{collection}/points"
    _http("PUT", url, {"points": points}, timeout=60)


def _run_pipeline(paths: list[str], ollama_url: str, qdrant_url: str,
                  collection: str, reset: bool) -> int:
    """Parse → summarize → embed → upsert. Returns number of chunks indexed."""
    # Parse
    files = parse_fortran._collect_files(paths)
    if not files:
        raise ValueError("No Fortran files found in provided paths")
    base_dir = parse_fortran._common_base(files)
    chunks: list[dict] = []
    for f in files:
        chunks.extend(parse_fortran.parse_file(f, base_dir))

    # Summarize
    summarized = []
    for chunk in chunks:
        summary = _generate(ollama_url, _build_prompt(chunk))
        summarized.append({**chunk, "summary": summary})

    # Embed
    embedded = []
    for chunk in summarized:
        vector = _embed(ollama_url, chunk.get("summary", ""))
        embedded.append({**chunk, "embedding": vector})

    # Upsert
    _ensure_collection(qdrant_url, collection, reset)
    points = [
        {
            "id": _point_id(c["source_file"], c["name"]),
            "vector": c["embedding"],
            "payload": {
                "name":        c["name"],
                "type":        c["type"],
                "source_file": c["source_file"],
                "line_start":  c["line_start"],
                "line_end":    c["line_end"],
                "summary":     c.get("summary", ""),
                "raw_code":    c["raw_code"],
                "calls":       c.get("calls", []),
            },
        }
        for c in embedded
    ]
    for start in range(0, len(points), _UPSERT_BATCH):
        _upsert_batch(qdrant_url, collection, points[start : start + _UPSERT_BATCH])

    return len(chunks)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str
    top: int = 5


class IndexRequest(BaseModel):
    paths: list[str]
    reset: bool = False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Fortran Modernizer Search API",
    description="Semantic search over a parsed and summarized Fortran codebase.",
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
def root():
    return FileResponse("app/static/index.html")


@app.get("/health")
def health():
    return {"status": "ok", "collection": COLLECTION}


@app.post("/search")
def search_endpoint(req: SearchRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    try:
        vector = _embed(OLLAMA_URL, req.query)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc}")

    url = QDRANT_URL.rstrip("/") + f"/collections/{COLLECTION}/points/search"
    try:
        result = _http("POST", url, {"vector": vector, "limit": req.top, "with_payload": True}, timeout=30)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Qdrant search failed: {exc}")

    results = []
    for hit in result.get("result", []):
        p = hit.get("payload", {})
        results.append({
            "score":       hit.get("score", 0.0),
            "name":        p.get("name"),
            "type":        p.get("type"),
            "source_file": p.get("source_file"),
            "line_start":  p.get("line_start"),
            "line_end":    p.get("line_end"),
            "summary":     p.get("summary"),
            "raw_code":    p.get("raw_code"),
            "calls":       p.get("calls", []),
        })

    return {"query": req.query, "results": results}


@app.post("/index")
def index_endpoint(req: IndexRequest):
    try:
        count = _run_pipeline(
            req.paths,
            ollama_url=OLLAMA_URL,
            qdrant_url=QDRANT_URL,
            collection=COLLECTION,
            reset=req.reset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {"status": "indexed", "chunks": count}
