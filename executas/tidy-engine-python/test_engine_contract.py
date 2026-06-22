#!/usr/bin/env python3
"""Contract tests for the tidy-engine source-of-truth guarantees.

Run: python test_engine_contract.py   (no dependencies, stdlib only)

These assert that every count the UI displays is computed deterministically by
the engine - the property that makes "N cells changed" trustworthy rather than
a model hallucination.
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("te", os.path.join(HERE, "tidy_engine_plugin.py"))
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)

# Isolate state to a temp dir so tests never touch ~/.anna.
_tmp = tempfile.mkdtemp(prefix="tidy-test-")
te.STATE_DIR = te.Path(_tmp)
te.STATE_FILE = te.STATE_DIR / "state.json"

RAW = (
    "Name, Date ,Amount,Plan\n"
    " alice , 01/05/2023,$1,200.50,pro\n"
    "BOB,2023-01-06, 900 ,FREE\n"
    " alice , 01/05/2023,$1,200.50,pro\n"
    ",,,\n"
)

passed = 0
def check(name, cond):
    global passed
    assert cond, f"FAILED: {name}"
    passed += 1
    print(f"  ok  {name}")

# describe contract: parameters must be a LIST of descriptors, not JSON-Schema
m = te.handle_describe({})
check("describe returns manifest with tools", bool(m.get("tools")))
params = m["tools"][0]["parameters"]
check("parameters is a list", isinstance(params, list))
check("parameter descriptors have name/type/description/required",
      all({"name", "type", "description", "required"} <= set(p) for p in params))

# invoke returns InvokeResult {success, data}
res = te.handle_invoke({"tool": "tidy", "arguments": {"action": "load", "raw_text": RAW}})
check("invoke wraps payload in success/data", res.get("success") is True and "data" in res)
sid = res["data"]["session_id"]

# detection
load = res["data"]
kinds = {i["kind"] for i in load["issues"]}
check("detects whitespace", "whitespace" in kinds)
check("detects empty rows", "empty_rows" in kinds)
check("detects dates", "dates" in kinds)
check("detects numbers", "numbers" in kinds)

# preview is non-committing
before = te.tool_tidy("get", session_id=sid)["preview"]["row_count"]
pv = te.tool_tidy("preview", session_id=sid, op={"type": "drop_empty_rows"})
after_preview = te.tool_tidy("get", session_id=sid)["preview"]["row_count"]
check("preview does not commit", before == after_preview)
check("preview reports a real row removal", pv["diff"]["rows_removed"] == 1)

# apply commits and counts exactly
ap = te.tool_tidy("apply", session_id=sid, op={"type": "trim_whitespace"})
check("trim changes the right number of cells", ap["diff"]["cells_changed"] == 5)
ap2 = te.tool_tidy("apply", session_id=sid, op={"type": "dedupe_rows"})
check("dedupe removes the duplicate after trim", ap2["diff"]["rows_removed"] == 1)

# undo restores prior state exactly
rc_before_undo = te.tool_tidy("get", session_id=sid)["preview"]["row_count"]
te.tool_tidy("undo", session_id=sid)
rc_after_undo = te.tool_tidy("get", session_id=sid)["preview"]["row_count"]
check("undo restores the removed row", rc_after_undo == rc_before_undo + 1)

# unknown op is rejected (never silently mutates)
bad = te.handle_invoke({"tool": "tidy", "arguments": {"action": "apply", "session_id": sid, "op": {"type": "nuke"}}})
check("unknown op type is rejected", bad.get("success") is False)

# date standardization is deterministic
te.tool_tidy("apply", session_id=sid, op={"type": "standardize_dates", "column": "Date"})
csv_out = te.tool_tidy("export", session_id=sid)["csv"]
check("dates normalized to ISO in export", "2023-01-05" in csv_out)

print(f"\n{passed} checks passed.")
