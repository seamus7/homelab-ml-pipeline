#!/usr/bin/env python3
"""
Automated Fortran-to-Python translation with numerical verification.

Usage:
    python3 parse_fortran.py file.f90 | python3 translate_fortran.py \\
        --ollama-url http://localhost:11434 --subroutine FACTORIAL
    python3 translate_fortran.py chunk.json
    python3 translate_fortran.py chunk.json --max-iterations 3
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_OLLAMA_URL = "http://ollama:11434"
_GEN_MODEL = "qwen3-coder-next"

# ---------------------------------------------------------------------------
# HTTP helper (consistent with the rest of the codebase)
# ---------------------------------------------------------------------------


def _http(method: str, url: str, body: dict | None = None, timeout: int = 120) -> dict:
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


def _generate(ollama_url: str, prompt: str) -> str:
    url = ollama_url.rstrip("/") + "/api/generate"
    result = _http(
        "POST",
        url,
        {"model": _GEN_MODEL, "prompt": prompt, "stream": False},
        timeout=180,
    )
    return result.get("response", "").strip()


# ---------------------------------------------------------------------------
# Response cleaning helpers
# ---------------------------------------------------------------------------


def _strip_think(text: str) -> str:
    """Remove <think>...</think> blocks that qwen3 models emit."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _clean_code(text: str) -> str:
    """Strip think tags and markdown code fences from a code response."""
    text = _strip_think(text)
    if text.startswith("```"):
        lines = text.splitlines()
        end = next(
            (i for i in range(1, len(lines)) if lines[i].startswith("```")),
            len(lines),
        )
        text = "\n".join(lines[1:end]).strip()
    return text


def _parse_json_response(text: str) -> list | dict:
    """Extract JSON from LLM response, tolerating think tags and fences."""
    text = _strip_think(text)
    # Strip markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        end = next(
            (i for i in range(1, len(lines)) if lines[i].startswith("```")),
            len(lines),
        )
        text = "\n".join(lines[1:end]).strip()
    # Find first [ or { and try to parse from there
    for i, ch in enumerate(text):
        if ch in ("[", "{"):
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                pass
    return json.loads(text)


# ---------------------------------------------------------------------------
# Source file resolution
# ---------------------------------------------------------------------------


def _resolve_source(source_file: str) -> str | None:
    """Resolve source_file against pod mount path or host-side fallback."""
    # Pod path
    pod = Path("/data/fortran") / source_file
    if pod.exists():
        return str(pod)

    # Host fallback: search under ../fortran-modernizer/ from scripts dir
    base = Path(__file__).parent.parent.parent / "fortran-modernizer"
    if base.exists():
        direct = base / source_file
        if direct.exists():
            return str(direct)
        # Recursive search by filename
        fname = Path(source_file).name
        for candidate in base.rglob(fname):
            return str(candidate)

    # Absolute path fallback
    if Path(source_file).exists():
        return source_file

    return None


# ---------------------------------------------------------------------------
# Driver comment cleaner
# ---------------------------------------------------------------------------


def _fix_driver_comments(src: str) -> str:
    """Replace fixed-format C comments with free-format ! comments."""
    lines = []
    for line in src.splitlines():
        if line and line[0].upper() == "C" and (len(line) == 1 or not line[1].isdigit()):
            lines.append("!" + line[1:])
        else:
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output name extraction
# ---------------------------------------------------------------------------


def _get_output_names(chunk: dict) -> list[str]:
    """Extract INTENT(OUT) and INTENT(INOUT) variable names from raw_code."""
    pattern = re.compile(
        r"INTENT\s*\(\s*(OUT|INOUT)\s*\)\s*::\s*([^\n]+)",
        re.IGNORECASE,
    )
    names = []
    for m in pattern.finditer(chunk["raw_code"]):
        vars_str = m.group(2)
        for var in vars_str.split(","):
            name = var.strip().split("(")[0].strip()  # strip array dims
            if name:
                names.append(name.upper())
    return names


# ---------------------------------------------------------------------------
# Standalone extraction
# ---------------------------------------------------------------------------


def _extract_standalone(chunk: dict) -> str:
    """
    Return a standalone Fortran source containing only the subroutine
    or function from chunk['raw_code'], stripped of any enclosing
    MODULE/CONTAINS wrapper. If already standalone, return as-is.
    """
    code = chunk["raw_code"]
    lines = code.splitlines()
    # Find the first SUBROUTINE or FUNCTION declaration line
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip().upper()
        if re.match(r"^\s*(SUBROUTINE|.*\bFUNCTION\b)", stripped):
            start = i
            break
    return "\n".join(lines[start:])


# ---------------------------------------------------------------------------
# generate_test_inputs
# ---------------------------------------------------------------------------


def generate_test_inputs(chunk: dict, ollama_url: str) -> list[dict]:
    """One LLM call: generate 5 sets of test inputs for INTENT(IN) arguments."""
    first_line = chunk["raw_code"].splitlines()[0] if chunk["raw_code"].strip() else ""
    prompt = (
        f"Given this Fortran subroutine/function:\n"
        f"Name: {chunk['name']}\n"
        f"Signature (first line): {first_line}\n"
        f"Full body:\n{chunk['raw_code']}\n\n"
        f"Generate 5 sets of test inputs as a JSON array.\n"
        f"Each set is a dict mapping argument name to numeric value.\n"
        f"Include only INTENT(IN) arguments.\n"
        f"Include at least one near-zero case and one typical case.\n"
        f"Return only the JSON array, no preamble, no markdown fences."
    )
    response = _generate(ollama_url, prompt)
    return _parse_json_response(response)


# ---------------------------------------------------------------------------
# generate_fortran_driver
# ---------------------------------------------------------------------------


def generate_fortran_driver(chunk: dict, test_input: dict, ollama_url: str) -> str:
    """One LLM call: generate a compilable Fortran PROGRAM that runs one test case."""
    source_file = chunk.get("source_file", "")
    is_function = chunk.get("type", "").lower() == "function"

    output_instructions = (
        "Write each INTENT(OUT) and INTENT(INOUT) variable to stdout:\n"
        "    WRITE(*,'(A,G30.15)') 'varname=', varname\n"
    )
    if is_function:
        output_instructions += (
            "This is a function — also print its return value using exactly:\n"
            "    WRITE(*,'(A,G30.15)') 'result=', <result_variable>\n"
        )

    prompt = (
        f"Generate a complete compilable Fortran PROGRAM to test this "
        f"subroutine/function.\n\n"
        f"Fortran source:\n{chunk['raw_code']}\n\n"
        f"Test inputs to assign: {json.dumps(test_input)}\n\n"
        f"Requirements:\n"
        f"1. Declare all variables with correct Fortran types matching "
        f"the source.\n"
        f"2. Assign the test input values listed above.\n"
        f"3. Call the subroutine/function directly by name.\n"
        f"4. {output_instructions}"
        f"5. IMPORTANT: Do NOT use any USE or MODULE statements. "
        f"The subroutine will be compiled and linked directly from its "
        f"source file.\n"
        f"6. Do not INCLUDE or IMPORT anything — just declare variables "
        f"and call the routine.\n"
    )

    if is_function:
        prompt += (
            "\nSince this is a FUNCTION (not a SUBROUTINE), declare an "
            "explicit interface block or declare the function return type "
            "before calling it.\n"
        )

    prompt += (
        "\nReturn only the complete Fortran PROGRAM source, "
        "no preamble, no markdown fences."
    )
    return _clean_code(_generate(ollama_url, prompt))


# ---------------------------------------------------------------------------
# run_fortran
# ---------------------------------------------------------------------------


def run_fortran(chunk: dict, driver_src: str) -> dict | None:
    """Compile driver + original source, run, parse stdout to dict."""
    source_path = _resolve_source(chunk.get("source_file", ""))
    tmpdir = tempfile.mkdtemp(prefix="fort_translate_")
    try:
        src_ext = Path(chunk.get("source_file", "routine.f90")).suffix or ".f90"
        driver_path = os.path.join(tmpdir, "driver.f90")
        runner_path = os.path.join(tmpdir, "runner")

        with open(driver_path, "w") as f:
            f.write(_fix_driver_comments(driver_src))

        standalone_src = _extract_standalone(chunk)
        standalone_path = os.path.join(tmpdir, f"routine{src_ext}")
        with open(standalone_path, "w") as f:
            f.write(standalone_src)
        cmd = ["gfortran", "-o", runner_path, driver_path, standalone_path]

        compile_result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if compile_result.returncode != 0:
            print(
                f"  [compile error]\n{compile_result.stderr[:800]}",
                file=sys.stderr,
            )
            return None

        run_result = subprocess.run(
            [runner_path], capture_output=True, text=True, timeout=10
        )
        if run_result.returncode != 0:
            print(
                f"  [runtime error] {run_result.stderr[:300]}",
                file=sys.stderr,
            )
            return None

        # Parse output: each line looks like "varname=   1.23456789012345E+00"
        output: dict = {}
        for line in run_result.stdout.splitlines():
            line = line.strip()
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                try:
                    output[key] = float(val)
                except ValueError:
                    output[key] = val
        return output if output else None

    except subprocess.TimeoutExpired:
        print("  [timeout] Fortran execution timed out", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"  [run_fortran error] {exc}", file=sys.stderr)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# translate_to_python
# ---------------------------------------------------------------------------


def translate_to_python(chunk: dict, ollama_url: str) -> str:
    """One LLM call: produce an initial Python translation."""
    prompt = (
        "Translate this Fortran subroutine to a Python function.\n"
        "Use only the Python standard library and math module.\n"
        "Preserve the algorithm exactly — do not simplify or optimize.\n"
        "Return only the Python function definition, no preamble,\n"
        "no markdown fences.\n"
        "If this is a subroutine (not a function), return all INTENT(OUT) "
        "and INTENT(INOUT) variables as a tuple in declaration order.\n\n"
        f"{chunk['raw_code']}"
    )
    return _clean_code(_generate(ollama_url, prompt))


# ---------------------------------------------------------------------------
# run_python
# ---------------------------------------------------------------------------


def run_python(python_src: str, test_input: dict, chunk: dict) -> dict | None:
    """exec() the translated Python, call the function, return outputs."""
    import inspect

    func_name = chunk["name"].lower()
    result_container: list = [None]
    exception_container: list = [None]

    def _run() -> None:
        ns: dict = {"math": math, "__builtins__": __builtins__}
        try:
            exec(python_src, ns)  # noqa: S102
        except Exception as exc:
            exception_container[0] = exc
            return

        # Find the function — exact match first, then case-insensitive
        func = ns.get(func_name)
        if func is None:
            for k, v in ns.items():
                if k.lower() == func_name and callable(v):
                    func = v
                    break
        if func is None:
            exception_container[0] = NameError(
                f"Function '{func_name}' not found in translated code"
            )
            return

        # Match test_input keys to function parameter names (case-insensitive)
        try:
            sig = inspect.signature(func)
            kwargs: dict = {}
            for param_name in sig.parameters:
                matched = test_input.get(param_name)
                if matched is None:
                    for k, v in test_input.items():
                        if k.lower() == param_name.lower():
                            matched = v
                            break
                if matched is not None:
                    kwargs[param_name] = matched
            ret = func(**kwargs) if kwargs else func(*list(test_input.values()))
        except Exception:
            # Positional fallback
            try:
                ret = func(*list(test_input.values()))
            except Exception as exc:
                exception_container[0] = exc
                return

        result_container[0] = ret

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)

    if t.is_alive():
        print("  [python timeout] execution exceeded 10s", file=sys.stderr)
        return None
    if exception_container[0] is not None:
        print(f"  [python error] {exception_container[0]}", file=sys.stderr)
        return None

    ret = result_container[0]
    is_function = chunk.get("type", "").lower() == "function"

    if ret is None:
        return {}
    if isinstance(ret, dict):
        return ret
    if is_function:
        if isinstance(ret, (int, float)):
            return {"result": float(ret)}
        return {"result": ret}
    # Subroutine: map return value(s) to INTENT(OUT) variable names
    out_names = _get_output_names(chunk)
    if isinstance(ret, (tuple, list)):
        if out_names and len(ret) == len(out_names):
            return {name: v for name, v in zip(out_names, ret)}
        return {f"out_{i}": v for i, v in enumerate(ret)}
    if isinstance(ret, (int, float)) and len(out_names) == 1:
        return {out_names[0]: float(ret)}
    return {"result": ret}


# ---------------------------------------------------------------------------
# compare_outputs
# ---------------------------------------------------------------------------


def compare_outputs(
    fortran_out: dict,
    python_out: dict,
    rtol: float = 1e-6,
    atol: float = 1e-10,
) -> tuple[bool, str]:
    """Compare Fortran ground-truth against Python output key by key."""
    if not fortran_out:
        return False, "Fortran output is empty"
    if not python_out:
        return False, "Python output is empty"

    diffs: list[str] = []
    for key, fval in fortran_out.items():
        # Exact key first, then case-insensitive
        pval = python_out.get(key)
        if pval is None:
            for k, v in python_out.items():
                if k.lower() == key.lower():
                    pval = v
                    break
        if pval is None:
            diffs.append(
                f"  key '{key}' in Fortran output but missing in Python output"
            )
            continue
        try:
            fv = float(fval)
            pv = float(pval)
            if not math.isclose(fv, pv, rel_tol=rtol, abs_tol=atol):
                diffs.append(
                    f"  {key}: Fortran={fv:.10g}  Python={pv:.10g}"
                    f"  (diff={abs(fv - pv):.3e})"
                )
        except (TypeError, ValueError):
            if str(fval).strip() != str(pval).strip():
                diffs.append(f"  {key}: Fortran={fval!r}  Python={pval!r}")

    if diffs:
        return False, "\n".join(diffs)
    return True, ""


# ---------------------------------------------------------------------------
# translate_subroutine  (main agentic loop)
# ---------------------------------------------------------------------------


def translate_subroutine(
    chunk: dict,
    ollama_url: str,
    max_iterations: int = 5,
) -> dict:
    name = chunk["name"]

    # ── Step 1: generate test inputs ─────────────────────────────────────────
    print(f"[{name}] Generating test inputs...", file=sys.stderr, flush=True)
    try:
        all_test_inputs: list[dict] = generate_test_inputs(chunk, ollama_url)
    except Exception as exc:
        return {"status": "error", "message": f"Failed to generate test inputs: {exc}"}
    print(
        f"[{name}] Got {len(all_test_inputs)} test input sets",
        file=sys.stderr,
        flush=True,
    )

    # ── Step 2: build ground truth via Fortran compilation ───────────────────
    valid_inputs: list[tuple[dict, dict]] = []
    for i, test_input in enumerate(all_test_inputs):
        print(
            f"[{name}] Ground-truth Fortran driver for input {i + 1}/{len(all_test_inputs)}...",
            file=sys.stderr,
            flush=True,
        )
        try:
            driver_src = generate_fortran_driver(chunk, test_input, ollama_url)
            fortran_out = run_fortran(chunk, driver_src)
        except Exception as exc:
            print(f"  [skip] {exc}", file=sys.stderr)
            continue
        if fortran_out is None:
            print(
                f"  [skip] Fortran run failed for input {i + 1}",
                file=sys.stderr,
                flush=True,
            )
            continue
        valid_inputs.append((test_input, fortran_out))
        print(f"  Ground truth: {fortran_out}", file=sys.stderr, flush=True)

    if not valid_inputs:
        return {"status": "compile_error", "name": name}

    # ── Step 3: initial translation ──────────────────────────────────────────
    print(f"[{name}] Translating to Python (initial)...", file=sys.stderr, flush=True)
    python_src = translate_to_python(chunk, ollama_url)

    # ── Step 4: agentic verification / correction loop ───────────────────────
    history: list[dict] = []

    for iteration in range(1, max_iterations + 1):
        print(
            f"[{name}] Iteration {iteration}/{max_iterations}...",
            file=sys.stderr,
            flush=True,
        )

        per_input_results: list[dict] = []
        all_pass = True

        for test_input, fortran_out in valid_inputs:
            python_out = run_python(python_src, test_input, chunk)
            ok, diff = compare_outputs(fortran_out, python_out or {})
            per_input_results.append(
                {
                    "test_input": test_input,
                    "fortran_out": fortran_out,
                    "python_out": python_out,
                    "match": ok,
                    "diff": diff,
                }
            )
            if not ok:
                all_pass = False

        history.append(
            {
                "iteration": iteration,
                "python_src": python_src,
                "results": per_input_results,
            }
        )

        passed = sum(1 for r in per_input_results if r["match"])
        print(
            f"[{name}] {passed}/{len(per_input_results)} inputs pass",
            file=sys.stderr,
            flush=True,
        )

        if all_pass:
            print(
                f"[{name}] All inputs match on iteration {iteration}!",
                file=sys.stderr,
                flush=True,
            )
            return {
                "status": "success",
                "name": name,
                "python_src": python_src,
                "iterations": iteration,
                "history": history,
            }

        # Build feedback for the next iteration
        failures = [r for r in per_input_results if not r["match"]]
        diff_summary = "\n".join(
            f"  Input {r['test_input']}: expected {r['fortran_out']}, got {r['python_out']}\n"
            f"  Diff:\n{r['diff']}"
            for r in failures
        )

        print(
            f"[{name}] {len(failures)} failure(s), requesting correction...",
            file=sys.stderr,
            flush=True,
        )

        feedback_prompt = (
            "This Fortran subroutine was translated to Python but the "
            "numerical outputs do not match. Here is the original Fortran:\n"
            f"{chunk['raw_code']}\n\n"
            "Your previous Python translation:\n"
            f"{history[-1]['python_src']}\n\n"
            "Test failures:\n"
            f"{diff_summary}\n\n"
            "Identify what went wrong and produce a corrected Python function.\n"
            "Return only the function definition, no preamble."
        )
        python_src = _clean_code(_generate(ollama_url, feedback_prompt))

    return {
        "status": "max_iterations_reached",
        "name": name,
        "history": history,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Translate a Fortran subroutine to Python with numerical verification."
    )
    ap.add_argument(
        "input",
        nargs="?",
        metavar="FILE",
        help="JSON file (single chunk or array of chunks) — default: stdin",
    )
    ap.add_argument(
        "--ollama-url",
        default=_DEFAULT_OLLAMA_URL,
        metavar="URL",
        help=f"Ollama base URL (default: {_DEFAULT_OLLAMA_URL})",
    )
    ap.add_argument(
        "--subroutine",
        metavar="NAME",
        help="Select subroutine/function by name from a multi-chunk input",
    )
    ap.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        metavar="N",
        help="Maximum correction iterations (default: 5)",
    )
    args = ap.parse_args()

    if args.input:
        data = json.loads(Path(args.input).read_text())
    else:
        if sys.stdin.isatty():
            ap.print_help()
            sys.exit(0)
        data = json.load(sys.stdin)

    chunks: list[dict] = [data] if isinstance(data, dict) else data

    if args.subroutine:
        target = args.subroutine.upper()
        chunks = [c for c in chunks if c.get("name", "").upper() == target]
        if not chunks:
            print(f"error: '{args.subroutine}' not found in input", file=sys.stderr)
            sys.exit(1)

    if not chunks:
        print("error: no chunks to process", file=sys.stderr)
        sys.exit(1)

    chunk = chunks[0]
    print(
        f"Processing: {chunk['name']} ({chunk.get('type', 'unknown')}) "
        f"from {chunk.get('source_file', '?')}",
        file=sys.stderr,
        flush=True,
    )

    result = translate_subroutine(chunk, args.ollama_url, args.max_iterations)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
