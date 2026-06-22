#!/usr/bin/env python3
"""
tidy-engine — Executa stdio tool plugin (single-dispatcher method)

The DETERMINISTIC source of truth for the TidyData Anna App. The host LLM only
*proposes* cleaning operations in plain English; this engine is the only thing
that ever reads, parses, transforms, or counts the user's data. Every number the
UI shows (rows removed, cells changed, duplicates found) is computed here — never
by the model — so a "47 cells changed" claim is real, not a hallucination.

Working tables are persisted to ``~/.anna/tidy-data/state.json`` keyed by a
session id, so a reload re-hydrates the in-progress clean without a backend.

Protocol: JSON-RPC 2.0 over stdio
Methods:  describe, invoke, health

One tool method (``tidy``) takes an ``action`` discriminator:
  load     raw_text                  -> session_id, headers, rows preview, issues
  suggest  session_id                -> deterministic suggested ops (safe baseline)
  preview  session_id, op            -> diff stats + resulting preview (NOT committed)
  apply    session_id, op            -> commit op, return new state + undo depth
  undo     session_id                -> revert last committed op
  export   session_id                -> cleaned CSV string
  get      session_id                -> current state
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Plugin manifest — Anna calls ``describe`` and uses this dict verbatim.
# NOTE: tools[].parameters MUST be a list of {name,type,description,required}
# descriptors. A JSON-Schema object here makes describe return "no manifest".
# ---------------------------------------------------------------------------
MANIFEST: dict[str, Any] = {
    "display_name": "Tidy Engine",
    "version": "1.0.0",
    "description": (
        "Deterministic spreadsheet/CSV cleaning engine. Parses raw tabular text, "
        "detects data-quality issues, and applies typed cleaning operations with "
        "exact before/after diffs. State persists to ~/.anna/tidy-data/state.json."
    ),
    "author": "TidyData",
    "homepage": "https://github.com/whtcjdtc2007/anna-executa-examples",
    "license": "MIT",
    "tags": ["productivity", "data", "csv", "cleaning", "anna-app"],
    "tools": [
        {
            "name": "tidy",
            "description": (
                "Clean tabular data. Use the `action` parameter to select an "
                "operation: load | suggest | preview | apply | undo | export | get. "
                "The engine is the source of truth: it returns exact counts of rows "
                "and cells changed; never invent these numbers yourself."
            ),
            "parameters": [
                {
                    "name": "action",
                    "type": "string",
                    "description": "One of: load, suggest, preview, apply, undo, export, get.",
                    "required": True,
                },
                {
                    "name": "session_id",
                    "type": "string",
                    "description": "Working-table id from a prior load. Required for all actions except load.",
                    "required": False,
                },
                {
                    "name": "raw_text",
                    "type": "string",
                    "description": "Raw CSV/TSV text. Required for action='load'.",
                    "required": False,
                },
                {
                    "name": "op",
                    "type": "object",
                    "description": (
                        "A cleaning operation for preview/apply. Shape: "
                        "{type, ...params}. Supported types: trim_whitespace, "
                        "drop_empty_rows, drop_empty_columns, dedupe_rows, "
                        "normalize_case{column,mode}, standardize_dates{column}, "
                        "normalize_numbers{column}, fill_blanks{column,value}, "
                        "split_column{column,delimiter,into[]}, rename_column{column,to}."
                    ),
                    "required": False,
                },
            ],
        },
    ],
    "runtime": {"type": "uv", "min_version": "0.1.0"},
}

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
STATE_DIR = Path(os.path.expanduser("~/.anna/tidy-data"))
STATE_FILE = STATE_DIR / "state.json"
MAX_SESSIONS = 25
MAX_UNDO = 25
PREVIEW_ROWS = 12


def _now() -> float:
    return time.time()


def _load_db() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"sessions": {}}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state.json root must be an object")
        data.setdefault("sessions", {})
        return data
    except (json.JSONDecodeError, ValueError) as e:
        backup = STATE_FILE.with_suffix(f".broken.{int(_now())}.json")
        try:
            STATE_FILE.rename(backup)
            print(f"[tidy-engine] corrupt state moved to {backup}: {e}", file=sys.stderr)
        except OSError:
            pass
        return {"sessions": {}}


def _save_db(db: dict[str, Any]) -> None:
    # Trim to the most-recently-touched sessions to bound the file size.
    sessions = db.get("sessions", {})
    if len(sessions) > MAX_SESSIONS:
        ordered = sorted(sessions.items(), key=lambda kv: kv[1].get("touched_at", 0), reverse=True)
        db["sessions"] = dict(ordered[:MAX_SESSIONS])
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    tmp.replace(STATE_FILE)


def _get_session(db: dict[str, Any], session_id: str) -> dict[str, Any]:
    sess = db.get("sessions", {}).get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id!r} (call action='load' first)")
    return sess


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _sniff_delimiter(sample: str) -> str:
    candidates = {",": 0, "\t": 0, ";": 0, "|": 0}
    first_lines = [ln for ln in sample.splitlines() if ln.strip()][:5]
    for ln in first_lines:
        for d in candidates:
            candidates[d] += ln.count(d)
    best = max(candidates, key=candidates.get)
    return best if candidates[best] > 0 else ","


def _parse_table(raw_text: str) -> tuple[list[str], list[list[str]]]:
    raw_text = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not raw_text.strip():
        raise ValueError("raw_text is empty — paste some CSV/TSV rows first")
    delim = _sniff_delimiter(raw_text)
    reader = csv.reader(io.StringIO(raw_text), delimiter=delim)
    rows = [list(r) for r in reader]
    if not rows:
        raise ValueError("no rows parsed")
    headers = [h.strip() for h in rows[0]]
    # de-blank header names so columns are addressable
    seen: dict[str, int] = {}
    for i, h in enumerate(headers):
        name = h or f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        headers[i] = name
    width = len(headers)
    body: list[list[str]] = []
    for r in rows[1:]:
        r = list(r)
        if len(r) < width:
            r = r + [""] * (width - len(r))
        elif len(r) > width:
            r = r[:width]
        body.append(r)
    return headers, body


# ---------------------------------------------------------------------------
# Issue detection (deterministic scan)
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%m-%Y",
    "%m/%d/%y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
    "%B %d %Y", "%b %d %Y", "%Y.%m.%d", "%m.%d.%Y",
]
_NUM_RE = re.compile(r"^\s*[-+]?[$€£¥]?\s*[\d,]*\.?\d+\s*%?\s*$")
_CURRENCY_RE = re.compile(r"[$€£¥,%]")


def _try_date(value: str) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _col(body: list[list[str]], idx: int) -> list[str]:
    return [r[idx] if idx < len(r) else "" for r in body]


def _detect_issues(headers: list[str], body: list[list[str]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    n = len(body)

    # whitespace
    ws = sum(1 for r in body for c in r if c != c.strip())
    if ws:
        issues.append({"kind": "whitespace", "count": ws,
                       "label": f"{ws} cell(s) have leading/trailing spaces"})

    # duplicate rows
    seen: set[tuple[str, ...]] = set()
    dups = 0
    for r in body:
        key = tuple(r)
        if key in seen:
            dups += 1
        else:
            seen.add(key)
    if dups:
        issues.append({"kind": "duplicates", "count": dups,
                       "label": f"{dups} duplicate row(s)"})

    # empty rows
    empty_rows = sum(1 for r in body if all((c or "").strip() == "" for c in r))
    if empty_rows:
        issues.append({"kind": "empty_rows", "count": empty_rows,
                       "label": f"{empty_rows} completely empty row(s)"})

    # per-column scans
    for i, h in enumerate(headers):
        col = _col(body, i)
        nonblank = [c for c in col if (c or "").strip() != ""]
        blanks = n - len(nonblank)
        if n and blanks and blanks < n:
            issues.append({"kind": "blanks", "column": h, "count": blanks,
                           "label": f"'{h}' has {blanks} blank cell(s)"})
        if not nonblank:
            issues.append({"kind": "empty_column", "column": h,
                           "label": f"'{h}' is completely empty"})
            continue
        # date-like
        date_hits = sum(1 for c in nonblank if _try_date(c))
        already_iso = sum(1 for c in nonblank if re.match(r"^\d{4}-\d{2}-\d{2}$", c.strip()))
        if date_hits >= max(2, int(0.6 * len(nonblank))) and already_iso < date_hits:
            issues.append({"kind": "dates", "column": h, "count": date_hits - already_iso,
                           "label": f"'{h}' has dates in mixed formats"})
        # numeric with currency/commas
        num_hits = sum(1 for c in nonblank if _NUM_RE.match(c) and _CURRENCY_RE.search(c))
        if num_hits >= max(2, int(0.5 * len(nonblank))):
            issues.append({"kind": "numbers", "column": h, "count": num_hits,
                           "label": f"'{h}' has numbers with currency/commas"})
        # inconsistent case (text columns)
        lowers = sum(1 for c in nonblank if c.isalpha() and c.islower())
        uppers = sum(1 for c in nonblank if c.isalpha() and c.isupper())
        titles = sum(1 for c in nonblank if c[:1].isupper() and not c.isupper())
        forms = sum(1 for x in (lowers, uppers, titles) if x > 0)
        if forms >= 2 and len(nonblank) >= 3:
            issues.append({"kind": "case", "column": h,
                           "label": f"'{h}' has inconsistent capitalization"})

    return issues


def _suggested_ops(headers: list[str], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic baseline ops derived from detected issues. Used as a safe
    fallback when the host LLM is unavailable (mock mode), and as a grounded
    starting point the model can re-order or explain."""
    ops: list[dict[str, Any]] = []
    kinds = {i["kind"] for i in issues}
    if "whitespace" in kinds:
        ops.append({"type": "trim_whitespace",
                    "why": "Strip leading/trailing spaces from every cell."})
    if "empty_rows" in kinds:
        ops.append({"type": "drop_empty_rows", "why": "Remove fully blank rows."})
    if "empty_column" in kinds:
        ops.append({"type": "drop_empty_columns", "why": "Remove columns with no data."})
    for i in issues:
        if i["kind"] == "dates":
            ops.append({"type": "standardize_dates", "column": i["column"],
                        "why": f"Convert '{i['column']}' to ISO YYYY-MM-DD."})
        elif i["kind"] == "numbers":
            ops.append({"type": "normalize_numbers", "column": i["column"],
                        "why": f"Strip currency/commas from '{i['column']}'."})
        elif i["kind"] == "case":
            ops.append({"type": "normalize_case", "column": i["column"], "mode": "title",
                        "why": f"Title-case the '{i['column']}' column."})
    if "duplicates" in kinds:
        ops.append({"type": "dedupe_rows", "why": "Remove duplicate rows (keep first)."})
    return ops


# ---------------------------------------------------------------------------
# Operations — each returns (new_headers, new_body). Pure functions.
# ---------------------------------------------------------------------------

def _require_col(headers: list[str], column: str) -> int:
    if column not in headers:
        raise ValueError(f"column not found: {column!r}")
    return headers.index(column)


def op_trim_whitespace(h, b, op):
    nb = [[c.strip() if isinstance(c, str) else c for c in r] for r in b]
    return h, nb


def op_drop_empty_rows(h, b, op):
    nb = [r for r in b if any((c or "").strip() != "" for c in r)]
    return h, nb


def op_drop_empty_columns(h, b, op):
    keep = [i for i, _ in enumerate(h) if any((r[i] or "").strip() != "" for r in b)] or list(range(len(h)))
    nh = [h[i] for i in keep]
    nb = [[r[i] for i in keep] for r in b]
    return nh, nb


def op_dedupe_rows(h, b, op):
    seen, nb = set(), []
    for r in b:
        key = tuple(r)
        if key not in seen:
            seen.add(key)
            nb.append(r)
    return h, nb


def op_normalize_case(h, b, op):
    idx = _require_col(h, op["column"])
    mode = op.get("mode", "title")
    def conv(s):
        if not isinstance(s, str) or not s.strip():
            return s
        if mode == "upper":
            return s.upper()
        if mode == "lower":
            return s.lower()
        return s.title()
    nb = [[conv(c) if i == idx else c for i, c in enumerate(r)] for r in b]
    return h, nb


def op_standardize_dates(h, b, op):
    idx = _require_col(h, op["column"])
    def conv(s):
        d = _try_date(s)
        return d if d else s
    nb = [[conv(c) if i == idx else c for i, c in enumerate(r)] for r in b]
    return h, nb


def op_normalize_numbers(h, b, op):
    idx = _require_col(h, op["column"])
    def conv(s):
        if not isinstance(s, str) or not s.strip():
            return s
        cleaned = s.replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("¥", "").replace("%", "").strip()
        try:
            f = float(cleaned)
            return str(int(f)) if f.is_integer() else str(f)
        except ValueError:
            return s
    nb = [[conv(c) if i == idx else c for i, c in enumerate(r)] for r in b]
    return h, nb


def op_fill_blanks(h, b, op):
    idx = _require_col(h, op["column"])
    value = str(op.get("value", ""))
    nb = [[(value if i == idx and (c or "").strip() == "" else c) for i, c in enumerate(r)] for r in b]
    return h, nb


def op_split_column(h, b, op):
    idx = _require_col(h, op["column"])
    delim = op.get("delimiter", " ")
    into = op.get("into") or [f"{op['column']}_1", f"{op['column']}_2"]
    k = len(into)
    nh = h[:idx] + list(into) + h[idx + 1:]
    nb = []
    for r in b:
        parts = (r[idx] or "").split(delim)
        parts = (parts + [""] * k)[:k]
        nb.append(r[:idx] + parts + r[idx + 1:])
    return nh, nb


def op_rename_column(h, b, op):
    idx = _require_col(h, op["column"])
    nh = list(h)
    nh[idx] = str(op.get("to", h[idx]))
    return nh, b


OPS = {
    "trim_whitespace": op_trim_whitespace,
    "drop_empty_rows": op_drop_empty_rows,
    "drop_empty_columns": op_drop_empty_columns,
    "dedupe_rows": op_dedupe_rows,
    "normalize_case": op_normalize_case,
    "standardize_dates": op_standardize_dates,
    "normalize_numbers": op_normalize_numbers,
    "fill_blanks": op_fill_blanks,
    "split_column": op_split_column,
    "rename_column": op_rename_column,
}


def _apply_op(headers, body, op) -> tuple[list[str], list[list[str]]]:
    if not isinstance(op, dict):
        raise ValueError("op must be an object")
    t = op.get("type")
    fn = OPS.get(t)
    if fn is None:
        raise ValueError(f"unknown op type: {t!r}; supported: {', '.join(sorted(OPS))}")
    return fn(headers, body, op)


def _diff_stats(h0, b0, h1, b1) -> dict[str, Any]:
    """Deterministic before/after counts — the anti-hallucination guarantee."""
    cells_changed = 0
    # cell-level diff only meaningful when shape comparable by row index + same headers
    if h0 == h1:
        for i in range(min(len(b0), len(b1))):
            r0, r1 = b0[i], b1[i]
            for j in range(min(len(r0), len(r1))):
                if r0[j] != r1[j]:
                    cells_changed += 1
    return {
        "rows_before": len(b0),
        "rows_after": len(b1),
        "rows_removed": max(0, len(b0) - len(b1)),
        "cols_before": len(h0),
        "cols_after": len(h1),
        "cols_removed": max(0, len(h0) - len(h1)),
        "cells_changed": cells_changed,
    }


def _preview(headers, body) -> dict[str, Any]:
    return {
        "headers": headers,
        "rows": body[:PREVIEW_ROWS],
        "row_count": len(body),
        "col_count": len(headers),
        "truncated": len(body) > PREVIEW_ROWS,
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_load(raw_text: str) -> dict[str, Any]:
    headers, body = _parse_table(raw_text)
    issues = _detect_issues(headers, body)
    session_id = uuid.uuid4().hex[:12]
    db = _load_db()
    db["sessions"][session_id] = {
        "headers": headers,
        "body": body,
        "original_headers": headers,
        "original_body": body,
        "undo_stack": [],
        "applied": [],
        "created_at": _now(),
        "touched_at": _now(),
    }
    _save_db(db)
    return {
        "session_id": session_id,
        "issues": issues,
        "issue_count": len(issues),
        "preview": _preview(headers, body),
    }


def _action_suggest(session_id: str) -> dict[str, Any]:
    db = _load_db()
    sess = _get_session(db, session_id)
    issues = _detect_issues(sess["headers"], sess["body"])
    ops = _suggested_ops(sess["headers"], issues)
    return {"session_id": session_id, "suggested_ops": ops, "issues": issues}


def _action_preview(session_id: str, op: dict[str, Any]) -> dict[str, Any]:
    db = _load_db()
    sess = _get_session(db, session_id)
    h1, b1 = _apply_op(sess["headers"], sess["body"], op)
    return {
        "op": op,
        "diff": _diff_stats(sess["headers"], sess["body"], h1, b1),
        "preview": _preview(h1, b1),
        "committed": False,
    }


def _action_apply(session_id: str, op: dict[str, Any]) -> dict[str, Any]:
    db = _load_db()
    sess = _get_session(db, session_id)
    h0, b0 = sess["headers"], sess["body"]
    h1, b1 = _apply_op(h0, b0, op)
    diff = _diff_stats(h0, b0, h1, b1)
    sess["undo_stack"].append({"headers": h0, "body": b0})
    sess["undo_stack"] = sess["undo_stack"][-MAX_UNDO:]
    sess["headers"], sess["body"] = h1, b1
    sess["applied"].append({"op": op, "diff": diff, "at": _now()})
    sess["touched_at"] = _now()
    _save_db(db)
    return {
        "op": op,
        "diff": diff,
        "preview": _preview(h1, b1),
        "applied_count": len(sess["applied"]),
        "undo_depth": len(sess["undo_stack"]),
        "committed": True,
    }


def _action_undo(session_id: str) -> dict[str, Any]:
    db = _load_db()
    sess = _get_session(db, session_id)
    if not sess["undo_stack"]:
        return {"message": "Nothing to undo.", "preview": _preview(sess["headers"], sess["body"]),
                "undo_depth": 0, "applied_count": len(sess["applied"])}
    prev = sess["undo_stack"].pop()
    sess["headers"], sess["body"] = prev["headers"], prev["body"]
    if sess["applied"]:
        sess["applied"].pop()
    sess["touched_at"] = _now()
    _save_db(db)
    return {"preview": _preview(sess["headers"], sess["body"]),
            "undo_depth": len(sess["undo_stack"]),
            "applied_count": len(sess["applied"])}


def _action_export(session_id: str) -> dict[str, Any]:
    db = _load_db()
    sess = _get_session(db, session_id)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(sess["headers"])
    w.writerows(sess["body"])
    return {
        "csv": out.getvalue(),
        "row_count": len(sess["body"]),
        "col_count": len(sess["headers"]),
        "applied": sess["applied"],
    }


def _action_get(session_id: str) -> dict[str, Any]:
    db = _load_db()
    sess = _get_session(db, session_id)
    return {
        "session_id": session_id,
        "preview": _preview(sess["headers"], sess["body"]),
        "issues": _detect_issues(sess["headers"], sess["body"]),
        "applied": sess["applied"],
        "undo_depth": len(sess["undo_stack"]),
    }


def tool_tidy(action: str, session_id: str = "", raw_text: str = "", op: Any = None) -> dict[str, Any]:
    if action == "load":
        return _action_load(raw_text)
    if action == "suggest":
        return _action_suggest(session_id)
    if action == "preview":
        return _action_preview(session_id, op or {})
    if action == "apply":
        return _action_apply(session_id, op or {})
    if action == "undo":
        return _action_undo(session_id)
    if action == "export":
        return _action_export(session_id)
    if action == "get":
        return _action_get(session_id)
    raise ValueError(
        f"unknown action: {action!r}; expected one of "
        "load | suggest | preview | apply | undo | export | get"
    )


TOOL_DISPATCH = {"tidy": tool_tidy}


# ---------------------------------------------------------------------------
# JSON-RPC handlers
# ---------------------------------------------------------------------------

def handle_describe(_params: dict[str, Any]) -> dict[str, Any]:
    return MANIFEST


def handle_invoke(params: dict[str, Any]) -> Any:
    tool_name = params.get("tool")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return {"success": False, "error": "`arguments` must be an object"}
    fn = TOOL_DISPATCH.get(tool_name)
    if fn is None:
        return {"success": False, "error": f"unknown tool: {tool_name!r}"}
    try:
        payload = fn(**args)
    except Exception as exc:  # surface tool errors via InvokeResult
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"success": True, "data": payload}


def handle_health(_params: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "state_file": str(STATE_FILE)}


METHOD_DISPATCH = {
    "describe": handle_describe,
    "invoke": handle_invoke,
    "health": handle_health,
}


# ---------------------------------------------------------------------------
# Stdio loop
# ---------------------------------------------------------------------------

def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    print(f"[tidy-engine] {MANIFEST['display_name']} v{MANIFEST['version']} ready", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": f"parse error: {e}"}})
            continue
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        handler = METHOD_DISPATCH.get(method)
        if handler is None:
            send({"jsonrpc": "2.0", "id": req_id,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})
            continue
        try:
            result = handler(params)
            send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:  # noqa: BLE001
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}})


if __name__ == "__main__":
    main()
