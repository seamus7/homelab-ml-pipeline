"""
FastAPI service exposing Fortran codebase search and indexing.

Configuration (environment variables):
  OLLAMA_URL    default http://ollama:11434
  QDRANT_URL    default http://qdrant:6333
  COLLECTION    default fortran_subroutines
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Import parse_fortran from sibling scripts/ directory
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import parse_fortran      # noqa: E402
import health_fortran     # noqa: E402
import translate_fortran  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OLLAMA_URL        = os.environ.get("OLLAMA_URL",        "http://ollama:11434")
QDRANT_URL        = os.environ.get("QDRANT_URL",        "http://qdrant:6333")
COLLECTION        = os.environ.get("COLLECTION",        "fortran_subroutines")
GITEA_URL         = os.getenv("GITEA_URL",         "http://gitea:3000")
GITEA_TOKEN       = os.getenv("GITEA_TOKEN",       "")
GITEA_REPO_OWNER  = os.getenv("GITEA_REPO_OWNER",  "admin")
GITEA_REPO_NAME   = os.getenv("GITEA_REPO_NAME",   "fortran-modernizer")

_GEN_MODEL   = "qwen3-coder-next"
_EMBED_MODEL = "nomic-embed-text"
_EMBED_DIM   = 768
_UPSERT_BATCH = 10

# ---------------------------------------------------------------------------
# HTTP helper  (copied from index_fortran.py)
# ---------------------------------------------------------------------------


def _http(method: str, url: str, body: dict | None = None, timeout: int = 60,
          extra_headers: dict | None = None) -> dict:
    """Minimal HTTP helper. Returns parsed JSON body. Raises RuntimeError on failure."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs: dict = {"Content-Type": "application/json"} if data else {}
    if extra_headers:
        hdrs.update(extra_headers)
    req = urllib.request.Request(
        url,
        data=data,
        headers=hdrs,
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


class HealthRequest(BaseModel):
    paths: list[str]
    pretty: bool = False


class TranslateRequest(BaseModel):
    path: str
    subroutine: str
    max_iterations: int = 5


class PRRequest(BaseModel):
    python_src: str
    subroutine_name: str
    source_file: str
    iterations: int
    explanation: str = ""


class AskRequest(BaseModel):
    query: str
    top: int = 5


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


@app.get("/documentation")
def docs_endpoint():
    url = QDRANT_URL.rstrip("/") + f"/collections/{COLLECTION}/points/scroll"
    all_points: list[dict] = []
    offset = None
    while True:
        body: dict = {"limit": 100, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        try:
            result = _http("POST", url, body, timeout=30)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=f"Qdrant scroll failed: {exc}")
        batch = result.get("result", {})
        all_points.extend(batch.get("points", []))
        offset = batch.get("next_page_offset")
        if offset is None:
            break

    subroutines = []
    for pt in all_points:
        p = pt.get("payload", {})
        subroutines.append({
            "name":        p.get("name"),
            "type":        p.get("type"),
            "source_file": p.get("source_file"),
            "line_start":  p.get("line_start"),
            "line_end":    p.get("line_end"),
            "summary":     p.get("summary", ""),
            "calls":       p.get("calls", []),
            "raw_code":    p.get("raw_code", ""),
        })

    by_file: dict[str, list[dict]] = {}
    for sub in subroutines:
        by_file.setdefault(sub["source_file"] or "unknown", []).append(sub)

    by_file_sorted = {
        k: sorted(v, key=lambda s: s["line_start"] or 0)
        for k, v in sorted(by_file.items())
    }

    alphabetical = sorted(subroutines, key=lambda s: (s["name"] or "").upper())

    return {
        "total":        len(subroutines),
        "by_file":      by_file_sorted,
        "alphabetical": alphabetical,
    }


@app.post("/health")
def code_health(req: HealthRequest):
    files = parse_fortran._collect_files(req.paths)
    if not files:
        raise HTTPException(status_code=400, detail="No Fortran files found in provided paths")
    base_dir = parse_fortran._common_base(files)
    chunks: list[dict] = []
    for f in files:
        chunks.extend(parse_fortran.parse_file(f, base_dir))
    if not chunks:
        raise HTTPException(status_code=400, detail="No subroutines or functions parsed from provided files")
    report = health_fortran.build_report(chunks)
    return report


@app.post("/translate")
def translate_endpoint(req: TranslateRequest):
    p = Path(req.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

    base_dir = parse_fortran._common_base([p])
    chunks = parse_fortran.parse_file(p, base_dir)

    target = req.subroutine.upper()
    chunk = next((c for c in chunks if c["name"].upper() == target), None)
    if chunk is None:
        raise HTTPException(
            status_code=404,
            detail=f"Subroutine '{req.subroutine}' not found in {req.path}",
        )

    health = health_fortran.analyse(chunk)
    if health["common_blocks"]:
        raise HTTPException(
            status_code=422,
            detail=(
                "Subroutine uses COMMON blocks — not supported in demo mode. "
                "Subroutines with empty common_blocks are supported."
            ),
        )

    return translate_fortran.translate_subroutine(chunk, OLLAMA_URL, req.max_iterations)


@app.post("/translate/pr")
def translate_pr_endpoint(req: PRRequest):
    auth = {"Authorization": f"token {GITEA_TOKEN}"}
    base = GITEA_URL.rstrip("/")
    owner = GITEA_REPO_OWNER
    repo  = GITEA_REPO_NAME

    branch_name = f"modernize/{req.subroutine_name.lower()}-{int(time.time())}"

    # 1. Create branch
    try:
        _http("POST", f"{base}/api/v1/repos/{owner}/{repo}/branches",
              {"new_branch_name": branch_name, "old_branch_name": "master"},
              extra_headers=auth)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"create branch failed: {exc}")

    # 2. Determine Python file path
    src = Path(req.source_file)
    stem = src.stem
    parent = str(src.parent)
    if parent == ".":
        py_filepath = f"modernized/{stem}_{req.subroutine_name.upper()}.py"
    else:
        py_filepath = f"modernized/{parent}/{stem}_{req.subroutine_name.upper()}.py"

    # 3. Create file on branch
    try:
        _http("POST", f"{base}/api/v1/repos/{owner}/{repo}/contents/{py_filepath}",
              {
                  "message": f"modernize: translate {req.subroutine_name} to Python ({req.iterations} iterations)",
                  "content": base64.b64encode(req.python_src.encode()).decode(),
                  "branch": branch_name,
              },
              extra_headers=auth)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"create file failed: {exc}")

    # 4. Open PR
    pr_body = (
        f"Automated translation of {req.subroutine_name} from {req.source_file}.\n\n"
        f"Completed in {req.iterations} iteration(s).\n\n"
        f"Summary: {req.explanation}\n\n"
        f"Generated by Fortran Modernizer — numerically verified."
    )
    try:
        pr = _http("POST", f"{base}/api/v1/repos/{owner}/{repo}/pulls",
                   {
                       "title": f"Modernize {req.subroutine_name}: Fortran to Python",
                       "body": pr_body,
                       "head": branch_name,
                       "base": "master",
                   },
                   extra_headers=auth)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"create PR failed: {exc}")

    return {"status": "ok", "pr_url": pr.get("html_url", "")}


@app.post("/ask")
def ask_endpoint(req: AskRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    try:
        vector = _embed(OLLAMA_URL, req.query)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc}")

    qdrant_url = QDRANT_URL.rstrip("/") + f"/collections/{COLLECTION}/points/search"
    try:
        result = _http("POST", qdrant_url,
                       {"vector": vector, "limit": req.top, "with_payload": True}, timeout=30)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Qdrant search failed: {exc}")

    sources = []
    for hit in result.get("result", []):
        p = hit.get("payload", {})
        sources.append({
            "name":        p.get("name"),
            "type":        p.get("type"),
            "source_file": p.get("source_file"),
            "line_start":  p.get("line_start"),
            "line_end":    p.get("line_end"),
            "summary":     p.get("summary", ""),
            "raw_code":    p.get("raw_code", ""),
        })

    context = "\n".join(
        f"Subroutine: {r['name']} ({r['type']}) in {r['source_file']} "
        f"lines {r['line_start']}-{r['line_end']}\n"
        f"Summary: {r['summary']}\n"
        f"Code:\n{r['raw_code']}\n"
        for r in sources
    )

    prompt = (
        "You are an expert in Fortran scientific computing analyzing a geodesy codebase. "
        "Answer the following question based only on the provided subroutine context. "
        "Be specific and technical. Cite which subroutines you are drawing from by name. "
        "If the answer cannot be determined from the provided context, say so explicitly "
        "rather than guessing.\n\n"
        f"Question: {req.query}\n\n"
        f"Relevant subroutines from the codebase:\n{context}"
    )

    def generate():
        ollama_url = OLLAMA_URL.rstrip("/") + "/api/generate"
        body = json.dumps({"model": _GEN_MODEL, "prompt": prompt, "stream": True}).encode()
        req_obj = urllib.request.Request(
            ollama_url, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        in_think = False
        buf = ""
        try:
            with urllib.request.urlopen(req_obj, timeout=300) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    buf += obj.get("response", "")
                    done = obj.get("done", False)

                    # Strip <think>...</think> blocks as tokens arrive
                    while True:
                        if not in_think:
                            idx = buf.find("<think>")
                            if idx == -1:
                                # Emit all but a possible partial tag at the tail
                                safe = len(buf)
                                for plen in range(min(7, len(buf)), 0, -1):
                                    if "<think>"[:plen] == buf[-plen:]:
                                        safe = len(buf) - plen
                                        break
                                if safe > 0:
                                    yield buf[:safe]
                                    buf = buf[safe:]
                                break
                            else:
                                if idx > 0:
                                    yield buf[:idx]
                                buf = buf[idx + 7:]
                                in_think = True
                        else:
                            idx = buf.find("</think>")
                            if idx == -1:
                                # Discard think content, keep possible partial close tag
                                safe_discard = len(buf)
                                for plen in range(min(8, len(buf)), 0, -1):
                                    if "</think>"[:plen] == buf[-plen:]:
                                        safe_discard = len(buf) - plen
                                        break
                                buf = buf[safe_discard:]
                                break
                            else:
                                buf = buf[idx + 8:]
                                in_think = False

                    if done:
                        if not in_think and buf:
                            yield buf
                        break
        except Exception as exc:
            yield f"\n[Stream error: {exc}]"

        yield "\n\n__SOURCES__\n" + json.dumps(sources)

    return StreamingResponse(generate(), media_type="text/plain")


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
