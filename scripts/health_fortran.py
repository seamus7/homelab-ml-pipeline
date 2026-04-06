#!/usr/bin/env python3
"""
Analyse parsed Fortran chunks for code health metrics.

Input:  JSON array from parse_fortran.py (stdin or file path argument)
Output: JSON health report with per-subroutine metrics and summary

Usage:
    python3 parse_fortran.py src/ | python3 health_fortran.py
    python3 parse_fortran.py src/ | python3 health_fortran.py --pretty
    python3 health_fortran.py chunks.json --pretty
"""

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

_RE_IMPLICIT_NONE = re.compile(r'\bIMPLICIT\s+NONE\b', re.IGNORECASE)

# Matches any GO TO / GOTO keyword (for counting)
_RE_GOTO = re.compile(r'\bGO\s*TO\b', re.IGNORECASE)

# Matches a line whose leading statement is GOTO/RETURN (unconditional)
# Optional leading line-label (digits), then the keyword.
_RE_UNCOND_GOTO   = re.compile(r'^\s*(?:\d+\s+)?GO\s*TO\b',  re.IGNORECASE)
_RE_UNCOND_RETURN = re.compile(r'^\s*(?:\d+\s+)?RETURN\b',    re.IGNORECASE)

# Detects an IF-condition prefix on the same line
_RE_IF_PREFIX = re.compile(r'\bIF\s*\(', re.IGNORECASE)

# COMMON /blockname/ — captures blockname
_RE_COMMON = re.compile(r'\bCOMMON\s*/\s*(\w+)\s*/', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Comment/string helpers — mirrored from parse_fortran.py
# ---------------------------------------------------------------------------

def _is_comment(line: str) -> bool:
    stripped = line.lstrip()
    if stripped.startswith('!'):
        return True
    if line and line[0].upper() in ('C', '*') and (len(line) < 2 or not line[1].isdigit()):
        return True
    return False


def _strip_inline_comment(line: str) -> str:
    result: list[str] = []
    in_string = False
    delim = ''
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
            elif ch == '!':
                break
            else:
                result.append(ch)
    return ''.join(result)

# ---------------------------------------------------------------------------
# Per-chunk analysis
# ---------------------------------------------------------------------------

def analyse(chunk: dict) -> dict:
    raw   = chunk['raw_code']
    lines = raw.splitlines()

    line_count  = len(lines)
    body_lines  = lines[1:-1]   # exclude declaration and END

    # ── implicit_none_missing ────────────────────────────────────────────────
    implicit_none_missing = not bool(_RE_IMPLICIT_NONE.search(raw))

    # ── goto_count ───────────────────────────────────────────────────────────
    goto_count = 0
    for line in body_lines:
        if not line.strip() or _is_comment(line):
            continue
        clean = _strip_inline_comment(line)
        goto_count += len(_RE_GOTO.findall(clean))

    # ── common_blocks ────────────────────────────────────────────────────────
    seen_names: set[str] = set()
    common_blocks: list[str] = []
    for line in body_lines:
        if _is_comment(line):
            continue
        clean = _strip_inline_comment(line)
        for m in _RE_COMMON.finditer(clean):
            name = m.group(1).upper()
            if name not in seen_names:
                seen_names.add(name)
                common_blocks.append(name)

    # ── has_dead_code ────────────────────────────────────────────────────────
    # Heuristic: if a standalone RETURN or unconditional GOTO is followed by
    # any executable (non-comment, non-blank) line within the same body, flag it.
    has_dead_code = False
    for i, line in enumerate(body_lines):
        if not line.strip() or _is_comment(line):
            continue
        clean = _strip_inline_comment(line).rstrip()
        # A line is unconditionally terminal if it starts with GOTO or RETURN
        # and does NOT have an IF(...) condition on the same line before it.
        is_uncond = (
            (bool(_RE_UNCOND_GOTO.match(clean)) or bool(_RE_UNCOND_RETURN.match(clean)))
            and not bool(_RE_IF_PREFIX.search(clean))
        )
        if is_uncond:
            for following in body_lines[i + 1:]:
                if following.strip() and not _is_comment(following):
                    has_dead_code = True
                    break
        if has_dead_code:
            break

    # ── no_comments ──────────────────────────────────────────────────────────
    no_comments = not any(_is_comment(line) for line in body_lines)

    return {
        'name':                 chunk['name'],
        'type':                 chunk['type'],
        'source_file':          chunk['source_file'],
        'line_start':           chunk['line_start'],
        'line_end':             chunk['line_end'],
        'line_count':           line_count,
        'implicit_none_missing': implicit_none_missing,
        'goto_count':           goto_count,
        'common_blocks':        common_blocks,
        'has_dead_code':        has_dead_code,
        'no_comments':          no_comments,
    }

# ---------------------------------------------------------------------------
# Report builder  (callable from app/main.py)
# ---------------------------------------------------------------------------

def build_report(chunks: list[dict]) -> dict:
    subroutines = [analyse(c) for c in chunks]

    total = len(subroutines)
    summary = {
        'total':                 total,
        'implicit_none_missing': sum(1 for s in subroutines if s['implicit_none_missing']),
        'has_goto':              sum(1 for s in subroutines if s['goto_count'] > 0),
        'has_common_blocks':     sum(1 for s in subroutines if s['common_blocks']),
        'has_dead_code':         sum(1 for s in subroutines if s['has_dead_code']),
        'no_comments':           sum(1 for s in subroutines if s['no_comments']),
        'needs_review':          sum(1 for s in subroutines if s['goto_count'] > 0 or s['common_blocks']),
        'needs_docs':            sum(1 for s in subroutines if s['no_comments']),
    }

    return {'subroutines': subroutines, 'summary': summary}

# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

_W = 70  # separator width

def print_pretty(report: dict) -> None:
    s     = report['summary']
    total = s['total']

    print(f"\n{'─' * _W}", file=sys.stderr)
    print(' Fortran Code Health Report', file=sys.stderr)
    print(f"{'─' * _W}", file=sys.stderr)
    print(f"  Subroutines analysed :  {total}", file=sys.stderr)
    print(f"  Missing IMPLICIT NONE:  {s['implicit_none_missing']:>3} / {total}", file=sys.stderr)
    print(f"  Has GOTO statements  :  {s['has_goto']:>3} / {total}", file=sys.stderr)
    print(f"  Has COMMON blocks    :  {s['has_common_blocks']:>3} / {total}", file=sys.stderr)
    print(f"  Potential dead code  :  {s['has_dead_code']:>3} / {total}", file=sys.stderr)
    print(f"  No inline comments   :  {s['no_comments']:>3} / {total}", file=sys.stderr)
    print(f"  Needs review (goto/common) : {s['needs_review']:>3} / {total}", file=sys.stderr)
    print(f"  Needs docs (no comments)   : {s['needs_docs']:>3} / {total}", file=sys.stderr)
    print(f"{'─' * _W}", file=sys.stderr)

    flagged = [
        sub for sub in report['subroutines']
        if (sub['implicit_none_missing'] or sub['goto_count'] > 0
                or sub['common_blocks'] or sub['has_dead_code'])
    ]

    if flagged:
        print(f"\n  Flagged ({len(flagged)} of {total}):", file=sys.stderr)
        for sub in flagged:
            flags: list[str] = []
            if sub['implicit_none_missing']:
                flags.append('no-implicit-none')
            if sub['goto_count'] > 0:
                flags.append(f"goto×{sub['goto_count']}")
            if sub['common_blocks']:
                flags.append('common:' + ','.join(sub['common_blocks']))
            if sub['has_dead_code']:
                flags.append('dead-code')
            loc = f"{sub['source_file']}:{sub['line_start']}"
            print(f"    {sub['name']:<30s}  {loc}", file=sys.stderr)
            print(f"      ↳ {' | '.join(flags)}", file=sys.stderr)

    print('', file=sys.stderr)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compute code health metrics for parsed Fortran chunks."
    )
    ap.add_argument(
        'input',
        nargs='?',
        metavar='FILE',
        help='JSON file produced by parse_fortran.py (default: stdin)',
    )
    ap.add_argument(
        '--pretty',
        action='store_true',
        help='Print human-readable summary to stderr after JSON output',
    )
    args = ap.parse_args()

    if args.input:
        data = json.loads(Path(args.input).read_text())
    else:
        if sys.stdin.isatty():
            ap.print_help()
            sys.exit(0)
        data = json.load(sys.stdin)

    if not isinstance(data, list):
        print("error: expected a JSON array of chunk objects", file=sys.stderr)
        sys.exit(1)

    report = build_report(data)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write('\n')

    if args.pretty:
        print_pretty(report)


if __name__ == '__main__':
    main()
