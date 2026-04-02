#!/usr/bin/env python3
"""
Parse Fortran source files and extract subroutines/functions as JSON chunks.

Each chunk contains: name, type, source_file, line_start, line_end,
raw_code, and calls (list of invoked routines).

Usage:
    python3 parse_fortran.py file.f90 [file2.f ...]
    python3 parse_fortran.py /path/to/src/dir/
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Intrinsics / keywords to suppress from call detection
# ---------------------------------------------------------------------------

_SKIP_CALLS: frozenset[str] = frozenset({
    # Control / IO keywords that look like calls
    "if", "else", "elseif", "end", "do", "while", "select", "case",
    "write", "read", "open", "close", "print", "stop", "return", "goto",
    "continue", "exit", "cycle", "allocate", "deallocate", "nullify",
    "format", "entry", "include",
    # Declaration keywords
    "intent", "implicit", "use", "module", "subroutine", "function",
    "program", "contains", "integer", "real", "complex", "logical",
    "character", "double", "precision", "type", "kind", "parameter",
    "dimension", "external", "intrinsic", "common", "equivalence",
    "data", "block",
    # Fortran intrinsic functions (non-exhaustive but covers the common set)
    "abs", "achar", "acos", "aimag", "aint", "all", "allocated", "anint",
    "any", "asin", "associated", "atan", "atan2", "bit_size", "btest",
    "ceiling", "char", "cmplx", "conjg", "cos", "cosh", "count", "cshift",
    "date_and_time", "dble", "dfloat", "digits", "dim", "dot_product",
    "dprod", "eoshift", "epsilon", "exp", "exponent", "float", "floor",
    "fraction", "huge", "iachar", "iand", "ibclr", "ibits", "ibset",
    "ichar", "idint", "ieor", "ifix", "index", "int", "ior", "ishft",
    "ishftc", "kind", "lbound", "len", "len_trim", "lge", "lgt", "lle",
    "llt", "log", "log10", "logical", "matmul", "max", "maxexponent",
    "maxloc", "maxval", "merge", "min", "minexponent", "minloc", "minval",
    "mod", "modulo", "mvbits", "nearest", "nint", "not", "null", "pack",
    "present", "product", "radix", "random_number", "random_seed", "range",
    "real", "repeat", "reshape", "rrspacing", "scale", "scan", "selected_int_kind",
    "selected_real_kind", "set_exponent", "shape", "sign", "sin", "sinh",
    "size", "sngl", "spacing", "spread", "sqrt", "sum", "system_clock",
    "tan", "tanh", "tiny", "transfer", "transpose", "trim", "ubound",
    "unpack", "verify",
})

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Optional type prefix before FUNCTION: INTEGER, REAL, DOUBLE PRECISION, etc.
_TYPE_PREFIX = r"(?:(?:INTEGER|REAL|DOUBLE\s+PRECISION|COMPLEX|LOGICAL|CHARACTER(?:\s*\*\s*\d+)?)\s+)?"

# Matches the opening line of a subroutine or function.
# Group 1: SUBROUTINE or FUNCTION
# Group 2: name
_RE_START = re.compile(
    r"^\s*" + _TYPE_PREFIX +
    r"(SUBROUTINE|FUNCTION)\s+(\w+)\s*"
    r"(?:\([^)]*\))?\s*"          # optional argument list
    r"(?:RESULT\s*\(\w+\))?\s*$", # optional RESULT clause
    re.IGNORECASE,
)

# Matches END SUBROUTINE [name] or END FUNCTION [name]
# Group 1: SUBROUTINE or FUNCTION; Group 2: optional name
_RE_END_TYPED = re.compile(
    r"^\s*END\s+(SUBROUTINE|FUNCTION)(?:\s+(\w+))?\s*$",
    re.IGNORECASE,
)

# Matches a bare END (old-style Fortran 77)
_RE_END_BARE = re.compile(r"^\s*END\s*$", re.IGNORECASE)

# CALL statement: CALL name(
_RE_CALL = re.compile(r"\bCALL\s+(\w+)\s*\(", re.IGNORECASE)

# Any identifier followed by ( — catches function-style invocations
_RE_FUNC_INVOKE = re.compile(r"\b(\w+)\s*\(", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_comment(line: str) -> bool:
    """Return True if this entire line is a Fortran comment."""
    stripped = line.lstrip()
    if stripped.startswith("!"):
        return True
    # Fixed-format: C or * in column 1 (column 1 = index 0, no leading spaces)
    if line and line[0].upper() in ("C", "*") and (len(line) < 2 or not line[1].isdigit()):
        return True
    return False


def _strip_inline_comment(line: str) -> str:
    """Remove everything from the first ! not inside a string literal."""
    result: list[str] = []
    in_string = False
    delim = ""
    for ch in line:
        if in_string:
            result.append(ch)
            if ch == delim:
                in_string = False
        else:
            if ch in ("'", '"'):
                in_string = True
                delim = ch
                result.append(ch)
            elif ch == "!":
                break
            else:
                result.append(ch)
    return "".join(result)


def _extract_calls(body_lines: list[str]) -> list[str]:
    """Return sorted, deduplicated list of called routine names from body_lines.

    body_lines[0] is the declaration line (SUBROUTINE/FUNCTION ...) and
    body_lines[-1] is the END line — neither contains executable calls.
    """
    found: set[str] = set()
    for raw in body_lines[1:-1]:  # skip declaration and END lines
        if _is_comment(raw):
            continue
        clean = _strip_inline_comment(raw).upper()

        for m in _RE_CALL.finditer(clean):
            name = m.group(1).lower()
            if name not in _SKIP_CALLS and not name.isdigit() and len(name) > 1:
                found.add(name)

        for m in _RE_FUNC_INVOKE.finditer(clean):
            name = m.group(1).lower()
            if name not in _SKIP_CALLS and not name.isdigit() and len(name) > 1:
                found.add(name)

    return sorted(found)


# ---------------------------------------------------------------------------
# Continuation-line preprocessor
# ---------------------------------------------------------------------------

def preprocess_lines(lines: list[str], is_fixed: bool) -> list[str]:
    """Return a list of the same length as `lines` with continuation lines joined.

    Continuation lines become empty strings; the joined logical line sits at
    the index of the first physical line of the group.  Line-number tracking
    in parse_file therefore stays accurate: lineno still maps 1-to-1 with the
    original file.

    Fixed-format (.f / .for): a non-space, non-'0' character at column index 5
    (0-based) marks a continuation.  Columns 0-5 of the continuation are stripped
    before joining.

    Free-format (.f90): a trailing & (after stripping inline comments) marks the
    current line as continued; the & is removed and the next line is joined.
    """
    result = list(lines)
    n = len(lines)
    i = 0

    if is_fixed:
        while i < n:
            raw = lines[i].rstrip('\n').rstrip('\r')
            # Skip comment lines and lines that are themselves continuations
            # (they get consumed below when we process the statement they belong to).
            if _is_comment(raw):
                i += 1
                continue
            if len(raw) > 5 and raw[5] not in (' ', '0'):
                # Orphaned continuation (shouldn't occur in well-formed code);
                # leave in place so it doesn't silently vanish.
                i += 1
                continue

            # Logical statement starts here — collect any following continuations.
            logical = raw
            j = i + 1
            while j < n:
                cont = lines[j].rstrip('\n').rstrip('\r')
                if _is_comment(cont):
                    j += 1          # comments may appear between continuations
                    continue
                if len(cont) > 5 and cont[5] not in (' ', '0'):
                    stmt = cont[6:] if len(cont) > 6 else ''
                    logical = logical.rstrip() + ' ' + stmt
                    result[j] = ''
                    j += 1
                else:
                    break
            result[i] = logical + '\n'
            i = j
    else:
        while i < n:
            raw = lines[i].rstrip('\n').rstrip('\r')
            clean = _strip_inline_comment(raw).rstrip()
            if not clean.endswith('&'):
                i += 1
                continue
            # Logical line continues onto the next physical line(s).
            logical = clean[:-1].rstrip()
            j = i + 1
            while j < n:
                cont = _strip_inline_comment(lines[j]).rstrip()
                if cont.endswith('&'):
                    logical += ' ' + cont[:-1].strip()
                    result[j] = ''
                    j += 1
                else:
                    logical += ' ' + cont.strip()
                    result[j] = ''
                    j += 1
                    break
            result[i] = logical + '\n'
            i = j

    return result


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_file(filepath: Path, base_dir: Path) -> list[dict]:
    """
    Parse one Fortran file. Returns a list of chunk dicts, one per
    subroutine/function found.
    """
    try:
        text = filepath.read_text(errors="replace")
    except OSError as exc:
        print(f"warning: cannot read {filepath}: {exc}", file=sys.stderr)
        return []

    lines = text.splitlines(keepends=True)
    try:
        rel_path = str(filepath.relative_to(base_dir))
    except ValueError:
        rel_path = str(filepath)

    is_fixed = filepath.suffix.lower() in ('.f', '.for')
    # proc_lines has the same length as lines; continuation groups are joined
    # onto the first physical line, consumed lines become ''.  All line-number
    # references below (start, lineno) still map correctly to the original file.
    proc_lines = preprocess_lines(lines, is_fixed)

    chunks: list[dict] = []
    # Stack entries: (unit_type, name, 1-based start line)
    scope_stack: list[tuple[str, str, int]] = []

    for lineno, proc in enumerate(proc_lines, start=1):
        if not proc.strip():
            continue
        if _is_comment(proc):
            continue

        clean = _strip_inline_comment(proc).rstrip()

        # --- Check for unit start ---
        m = _RE_START.match(clean)
        if m:
            unit_type = m.group(1).upper()
            unit_name = m.group(2)
            scope_stack.append((unit_type, unit_name, lineno))
            continue

        # --- Check for typed END ---
        m = _RE_END_TYPED.match(clean)
        if m:
            if scope_stack:
                open_type, open_name, start = scope_stack.pop()
                body = lines[start - 1 : lineno]
                chunks.append({
                    "name": open_name,
                    "type": open_type.lower(),
                    "source_file": rel_path,
                    "line_start": start,
                    "line_end": lineno,
                    "raw_code": "".join(body),
                    "calls": _extract_calls(body),
                })
            continue

        # --- Check for bare END (closes innermost open scope, if any) ---
        if _RE_END_BARE.match(clean) and scope_stack:
            open_type, open_name, start = scope_stack.pop()
            body = lines[start - 1 : lineno]
            chunks.append({
                "name": open_name,
                "type": open_type.lower(),
                "source_file": rel_path,
                "line_start": start,
                "line_end": lineno,
                "raw_code": "".join(body),
                "calls": _extract_calls(body),
            })

    return chunks


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

_FORTRAN_EXTS = {".f90", ".f", ".for"}


def _collect_files(paths: list[str]) -> list[Path]:
    result: list[Path] = []
    for p in paths:
        path = Path(p).resolve()
        if path.is_dir():
            for ext in _FORTRAN_EXTS:
                result.extend(sorted(path.rglob(f"*{ext}")))
        elif path.is_file():
            if path.suffix.lower() in _FORTRAN_EXTS:
                result.append(path)
            else:
                print(f"warning: skipping {p} (unrecognised extension)", file=sys.stderr)
        else:
            print(f"warning: {p} does not exist", file=sys.stderr)
    return result


def _common_base(files: list[Path]) -> Path:
    """Return the deepest common directory of all given file paths."""
    parents = [f.parent for f in files]
    if len(parents) == 1:
        return parents[0]
    return Path(os.path.commonpath([str(p) for p in parents]))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract subroutines/functions from Fortran source as JSON."
    )
    ap.add_argument(
        "paths",
        nargs="+",
        help="Fortran source files (.f90 / .f / .for) or directories to scan",
    )
    args = ap.parse_args()

    files = _collect_files(args.paths)
    if not files:
        print("error: no Fortran files found", file=sys.stderr)
        sys.exit(1)

    base_dir = _common_base(files)

    all_chunks: list[dict] = []
    for f in files:
        all_chunks.extend(parse_file(f, base_dir))

    json.dump(all_chunks, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
