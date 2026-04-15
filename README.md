# Fortran Modernizer

An internal tooling demo for Fortran code intelligence. Given a legacy Fortran codebase, this service parses it into subroutine-level chunks, generates plain-English summaries via a local LLM, indexes them in a vector store, and exposes five features through a web UI and REST API: semantic search, code health analysis, auto-documentation, agentic translation to Python with numerical verification, and RAG-based Q&A.

Built as a portfolio piece demonstrating a self-hosted ML inference pipeline on k3s. Default inference runs locally on an RTX 5090 via Ollama — no API keys required. Cloud models (Claude Sonnet, GLM) are available via OpenRouter through LiteLLM with a single environment variable change.

---

## Stack

| Component | Role | Why |
|---|---|---|
| **k3s** | Kubernetes runtime | Lightweight; runs on a single node, second node (RTX 4080) joins later |
| **Ollama** | Local LLM inference | Runs `qwen3-coder-next` (generation) and `nomic-embed-text` (embeddings) on RTX 5090 |
| **Qdrant** | Vector store | Fast cosine similarity search; simple REST API; no dependency on Python clients |
| **FastAPI + uvicorn** | API server | Async, clean OpenAPI docs, minimal boilerplate |
| **Gitea** | Self-hosted Git | PR creation target for translation output; acts as the output registry |
| **Traefik** | Ingress | k3s default; zero-config routing to `modernizer.homelab.local` |
| **LiteLLM** | Model-agnostic inference proxy | Routes generation requests to local Ollama, OpenRouter, or AWS Bedrock without code changes — swap providers via environment variable |
| **gfortran** | Fortran compiler | Used inside the container for numerical verification during translation |

The service is a single container (`modernizer:latest`) with no external dependencies beyond the k3s cluster services.

---

## Model Configuration

The pipeline is model-agnostic via LiteLLM. Generation requests route through `http://litellm:4000` inside the cluster. Switch providers by changing two environment variables — no code changes required.

| `INFERENCE_MODEL` | Routes to | Cost |
|---|---|---|
| `default` | qwen3-coder-next via local Ollama | Free (local GPU) |
| `sonnet` | Claude Sonnet 4.6 via OpenRouter | ~$0.005/subroutine |
| `glm` | GLM-5.1 via OpenRouter | ~$0.002/subroutine |

To run the summarization pipeline against a different model:
```bash
export INFERENCE_URL=http://localhost:4000  # port-forwarded LiteLLM
export INFERENCE_MODEL=sonnet
python3 scripts/index_fortran.py /path/to/fortran/ --reset
```

**Quality comparison — GRVINT (opaque F77, no comments):**

qwen3-coder-next (local): Correctly identifies inverse-distance weighting, coincidence guard, and output variable. Good summary.

Claude Sonnet: Same accuracy plus names the reason for the coincidence guard ("to avoid division by zero"), explicitly describes weight normalization, and caught a mixed single/double precision declaration that qwen missed. Noticeably more precise on technical detail.

For a 5,000-subroutine production codebase: ~$25 to index with Sonnet vs free with local qwen. The choice is a config change.

---

## Architecture

```
Browser / curl
     │
     ▼
FastAPI (modernizer:8000)
     │
     ├── parse_fortran.py      ← regex-based AST extraction
     ├── health_fortran.py     ← static analysis metrics
     └── translate_fortran.py  ← agentic LLM + gfortran loop
     │
     ├── LiteLLM (inference proxy → Ollama / OpenRouter / Bedrock)
     ├── Ollama  (qwen3-coder-next, nomic-embed-text — default backend)
     ├── Qdrant  (vector index of subroutine summaries)
     └── Gitea   (PR creation for translated Python)
```

Fortran source is mounted read-only into the pod at `/data/fortran`. The indexing pipeline (parse → summarize → embed → upsert) runs on demand via `POST /index` or directly via `scripts/index_fortran.py`.

---

## Features

### 1. Semantic Search

**What it does:** Embeds a plain-English query with `nomic-embed-text`, runs cosine similarity search against the Qdrant collection, and returns the top-N subroutines with their LLM-generated summaries and source code.

**Endpoint:** `POST /search`

**UI tab:** Search

**Example:**
```json
POST /search
{"query": "coordinate transformation from geodetic to Cartesian", "top": 5}
```

Returns ranked results with score, subroutine name, source location, summary, and raw code.

**CLI equivalent:**
```bash
python3 scripts/query_fortran.py coordinate transformation from geodetic to Cartesian
python3 scripts/query_fortran.py --top 3 gravity anomaly computation
```

---

### 2. Code Health Report

**What it does:** Parses one or more Fortran source paths and runs static analysis on every subroutine. Reports per-routine metrics and an aggregate summary.

**Metrics collected per subroutine:**

| Metric | Description |
|---|---|
| `implicit_none_missing` | No `IMPLICIT NONE` declaration |
| `goto_count` | Number of `GOTO` / `GO TO` statements |
| `common_blocks` | Named COMMON blocks used |
| `has_dead_code` | Heuristic: executable code follows an unconditional `RETURN` or `GOTO` |
| `no_comments` | No comment lines in the body |

**Endpoint:** `POST /health` (body), `GET /health` (liveness check)

**UI tab:** Health

**Example:**
```json
POST /health
{"paths": ["/data/fortran/src"], "pretty": false}
```

Returns `{"subroutines": [...], "summary": {"total": 42, "has_goto": 11, ...}}`.

**CLI equivalent:**
```bash
python3 scripts/parse_fortran.py src/ | python3 scripts/health_fortran.py --pretty
```

---

### 3. Auto Documentation

**What it does:** Scrolls the entire Qdrant collection and returns all indexed subroutines organized two ways: grouped by source file (sorted by line number) and alphabetically. Each entry includes the LLM-generated summary.

This is a read-only view of what was indexed. It requires the codebase to have been indexed first.

**Endpoint:** `GET /documentation`

**UI tab:** Docs

**Example output structure:**
```json
{
  "total": 87,
  "by_file": {
    "geodesy/coord.f90": [
      {"name": "GEOD2CART", "line_start": 12, "line_end": 58, "summary": "..."}
    ]
  },
  "alphabetical": [...]
}
```

---

### 4. Translation (Fortran → Python with Gitea PR)

**What it does:** Translates a single named subroutine to Python using an agentic loop:

1. LLM generates 5 sets of test inputs for `INTENT(IN)` arguments
2. LLM generates a Fortran driver program for each test case
3. `gfortran` compiles and runs the driver to produce ground-truth outputs
4. LLM produces an initial Python translation
5. Python translation is executed and outputs compared against ground truth (`rtol=1e-6`, `atol=1e-10`)
6. If any test fails, the LLM receives the diff and produces a corrected translation
7. Repeat up to `max_iterations` (default: 5)

Each iteration records the Python source, per-input pass/fail results, and a one-sentence LLM explanation of what changed. On success, the final result includes status, iteration count, explanation, and full history.

**Constraint:** Subroutines using COMMON blocks are rejected (`422 Unprocessable Entity`). See Known Limitations.

**Endpoint:** `POST /translate`

**UI tab:** Translate

**Example:**
```json
POST /translate
{
  "path": "/data/fortran/src/geodesy/coord.f90",
  "subroutine": "GRVINT",
  "max_iterations": 5
}
```

After a successful translation, the UI offers a **Create PR** button that calls `POST /translate/pr`. This:
- Creates a new branch `modernize/<name>-<timestamp>` in the configured Gitea repo
- Commits the translated Python file to `modernized/<path>/<FILE>_<SUBROUTINE>.py`
- Opens a PR with title `Modernize <NAME>: Fortran to Python` and a body summarizing iterations and the explanation

**Gitea config** is set via environment variables: `GITEA_URL`, `GITEA_TOKEN`, `GITEA_REPO_OWNER`, `GITEA_REPO_NAME`.

---

### 5. RAG Ask

**What it does:** Answers natural-language questions about the codebase by retrieving the top-N relevant subroutines from Qdrant (same embedding search as Semantic Search), injecting their summaries and raw code as context, and streaming a response from the LLM. The LLM is instructed to cite subroutine names and say explicitly when the answer cannot be determined from the provided context.

Streaming response is `text/plain`. The final chunk appended to the stream is:
```
__SOURCES__
[{"name": "...", "source_file": "...", ...}, ...]
```

The UI parses this delimiter to render cited sources separately below the answer.

The stream filters out `<think>...</think>` blocks emitted by `qwen3` models so reasoning tokens are not shown to the user.

**Endpoint:** `POST /ask`

**UI tab:** Ask

**Example:**
```json
POST /ask
{"query": "How does the codebase handle ellipsoid flattening?", "top": 5}
```

---

## Scripts Reference

| Script | Purpose | Input | Output |
|---|---|---|---|
| `parse_fortran.py` | Extract subroutines/functions as JSON chunks | Fortran files or directories | JSON array of chunk objects |
| `health_fortran.py` | Static analysis metrics on parsed chunks | JSON chunks (stdin or file) | JSON health report |
| `summarize_fortran.py` | Annotate chunks with LLM summaries | JSON chunks → Ollama | JSON chunks with `summary` field |
| `embed_fortran.py` | Generate embeddings for chunk summaries | Summarized JSON chunks → Ollama | JSON chunks with `embedding` field |
| `index_fortran.py` | Full pipeline: parse → summarize → embed → upsert | Fortran files or directories | Chunks upserted to Qdrant |
| `query_fortran.py` | CLI semantic search against Qdrant index | Plain-English query string | Formatted terminal output |
| `translate_fortran.py` | Agentic Fortran → Python translation | Single chunk JSON or stdin | JSON with status, python_src, history |

### parse_fortran.py

```bash
# Parse a directory, write JSON to stdout
python3 scripts/parse_fortran.py /path/to/src/

# Parse specific files
python3 scripts/parse_fortran.py routines.f90 helpers.f

# Pipe into health check
python3 scripts/parse_fortran.py src/ | python3 scripts/health_fortran.py --pretty
```

Handles both fixed-format (`.f`, `.for`) and free-format (`.f90`) Fortran. Continuation lines are preprocessed before parsing. Extracts `SUBROUTINE` and `FUNCTION` units; skips `PROGRAM` blocks.

### index_fortran.py

```bash
# Index a codebase (incremental — adds to existing collection)
python3 scripts/index_fortran.py /path/to/src/ \
    --ollama-url http://localhost:11434 \
    --qdrant-url http://localhost:6333

# Reset collection and reindex
python3 scripts/index_fortran.py /path/to/src/ --reset

# Custom collection name
python3 scripts/index_fortran.py /path/to/src/ --collection my_collection
```

Defaults: Ollama at `http://ollama:11434`, Qdrant at `http://qdrant:6333`, collection `fortran_subroutines`.

### query_fortran.py

```bash
# Multi-word query without quotes
python3 scripts/query_fortran.py find routines that handle gravity calculations

# Limit results
python3 scripts/query_fortran.py --top 3 coordinate transformation

# Against local Qdrant
python3 scripts/query_fortran.py --qdrant-url http://localhost:6333 ellipsoid parameters
```

### translate_fortran.py

```bash
# Translate from parsed JSON on stdin
python3 scripts/parse_fortran.py src/geodesy.f90 | \
    python3 scripts/translate_fortran.py --subroutine GRVINT

# From a chunk JSON file
python3 scripts/translate_fortran.py chunk.json --max-iterations 3

# Against local Ollama
python3 scripts/translate_fortran.py chunk.json \
    --ollama-url http://localhost:11434 \
    --subroutine FACTORIAL
```

---

## Setup and Deployment

### Prerequisites

- k3s running with Ollama, Qdrant, and Gitea deployed in the cluster
- Ollama models pulled: `ollama pull qwen3-coder-next && ollama pull nomic-embed-text`
- Gitea repo created for PR output (configured via env vars)

### Build and deploy

```bash
# Build the container image (runs on the k3s node)
docker build -t modernizer:latest .

# Create the Gitea token secret
kubectl create secret generic gitea-token --from-literal=token=<your-token>

# Add OPENROUTER_API_KEY to .env, then:
source .env && bash k8s/create-secrets.sh

# Apply the manifest
kubectl apply -f k8s/modernizer.yaml

# Verify
kubectl rollout status deployment/modernizer
kubectl get pods -l app=modernizer
```

The pod mounts `/home/francis/repos/fortran-modernizer` from the host at `/data/fortran`. Adjust the `hostPath` in `k8s/modernizer.yaml` to point at your Fortran source tree.

### Index a codebase

Via API (recommended — runs inside the pod, resolves paths relative to the mount):
```bash
curl -X POST http://modernizer.homelab.local/index \
  -H 'Content-Type: application/json' \
  -d '{"paths": ["/data/fortran/src"], "reset": true}'
```

Via script (for local dev, requires Ollama and Qdrant reachable):
```bash
python3 scripts/index_fortran.py /path/to/fortran/src/ \
    --ollama-url http://localhost:11434 \
    --qdrant-url http://localhost:6333 \
    --reset
```

### Local development

```bash
# Install dependencies
pip install fastapi "uvicorn[standard]"

# Run with services forwarded
kubectl port-forward svc/ollama 11434:11434 &
kubectl port-forward svc/qdrant 6333:6333 &

OLLAMA_URL=http://localhost:11434 \
QDRANT_URL=http://localhost:6333 \
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The UI is served at `http://localhost:8000`. OpenAPI docs at `http://localhost:8000/docs`.

---

## Known Limitations

**Array indexing false positives in calls list**
The `calls` field in parsed chunks is extracted by matching any `identifier(` pattern that isn't in the intrinsic/keyword skip list. Array accesses like `A(I)` or `B(K,J)` are indistinguishable from function calls at the regex level. This means `calls` may include array variable names. The summaries and search quality are not meaningfully affected, but the calls graph is not reliable for dependency analysis.

**Monolithic PROGRAM blocks not indexed**
The parser extracts `SUBROUTINE` and `FUNCTION` units only. Top-level `PROGRAM` blocks are skipped. Legacy Fortran 77 codebases that put logic directly in a `PROGRAM` rather than decomposing into subroutines will produce zero chunks from those files.

**COMMON block translation (Level 1 only)**
Translation is blocked for any subroutine that uses named COMMON blocks (`/BLOCKNAME/`). The `POST /translate` endpoint returns `422` in this case. Translating COMMON blocks requires understanding the shared state across all subroutines that reference the same block — that context is not yet threaded through the translation prompt. Subroutines with no COMMON usage translate normally.

**Fixed vs free format edge cases**
Format detection is based on file extension (`.f` / `.for` = fixed, `.f90` = free). Files with non-standard extensions or mixed-format content will be parsed incorrectly. Fixed-format continuation detection requires a non-space, non-`0` character at column index 5 (0-based); files where continuation markers land elsewhere will have their continuations missed and may produce malformed chunks.

---

## Roadmap

**Feature 5: Performance Analysis**
Static and LLM-assisted analysis targeting computational efficiency. Would cover: loop trip counts and nesting depth, identification of hot-path subroutines via call graph traversal from indexed `calls` data, memory access pattern flags (e.g., column-major vs row-major array traversal in Fortran), detection of vectorization-hostile patterns (GOTO inside DO loops, mixed precision arithmetic), and a summary recommendation per routine (e.g., "candidate for BLAS replacement", "loop reordering suggested").

**Whole-file translation**
Currently translation handles one subroutine at a time. Whole-file translation would resolve dependencies between subroutines in the same file, handle shared type declarations, and produce a coherent Python module rather than isolated functions.

**Fortran cleanup (GOTO removal, refactoring with numerical verification)**
A pre-translation step that rewrites legacy Fortran in place: GOTO → structured control flow, implicit typing → explicit declarations, identified dead code removal. Each rewrite step would be verified numerically using the same gfortran driver approach already used in translation.

**Gitea container registry mirroring**
Automate mirroring of the `modernizer:latest` image into the Gitea container registry so the k8s manifest can pull from the internal registry rather than requiring the image to be pre-built on the node with `imagePullPolicy: Never`.

