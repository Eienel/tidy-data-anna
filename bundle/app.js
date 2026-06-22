/**
 * TidyData — Anna App bundle controller
 *
 * Connects to Anna via the runtime SDK (global: AnnaAppRuntime).
 *
 * RPC shapes used (verified against the focus-flow reference app):
 *   anna.tools.invoke({ tool_id, method: "tidy", args: { action, ... } })
 *                       -> { success: true, data: <payload> }
 *   anna.llm.complete({ messages: [{role, content}] })   (best-effort; optional)
 *   anna.storage.get / set ({ key, value })
 *   anna.chat.write_message({ role, content })
 *   anna.window.set_title({ title })
 *
 * ARCHITECTURE GUARANTEE: the tidy-engine Executa is the ONLY thing that reads,
 * transforms, or counts the user's data. The LLM only *proposes* operations in
 * plain English; every op is validated against the engine's op vocabulary,
 * previewed by the engine (real diff), and applied only on explicit approval.
 * If the host LLM is unavailable, we fall back to the engine's deterministic
 * `suggest` action, so the app works fully offline (anna-app dev --no-llm).
 */

import { AnnaAppRuntime } from "/static/anna-apps/_sdk/latest/index.js";

const DEV_FALLBACK_TOOL_ID = "tool-eienel-tidy-engine-84txvjcy";
const TOOL_ID =
  (typeof window !== "undefined"
    && window.__ANNA_TOOL_IDS__
    && window.__ANNA_TOOL_IDS__["tidy-engine"])
  || DEV_FALLBACK_TOOL_ID;
const TOOL_METHOD = "tidy";
const STORAGE_KEY = "tidy-data:session";

// The op vocabulary the engine understands. Used to validate anything the LLM
// proposes — unknown shapes are dropped before they ever reach the engine.
const OP_TYPES = new Set([
  "trim_whitespace", "drop_empty_rows", "drop_empty_columns", "dedupe_rows",
  "normalize_case", "standardize_dates", "normalize_numbers", "fill_blanks",
  "split_column", "rename_column",
]);

const SAMPLE = `Full Name, Signup Date , Spend ,Plan,Plan
 alice cooper ,01/05/2023,$1,240.00,pro,pro
BOB DYLAN, 2023-01-06 ,990,FREE,FREE
 alice cooper ,01/05/2023,$1,240.00,pro,pro
joni mitchell,Jan 7 2023, 2,030.50 ,Pro,Pro
,,,,
NEIL young , 2023/01/08,1170,free,free`;

const $ = (s) => document.querySelector(s);

const els = {
  body: document.body,
  status: $("#status-label"),
  conn: $("#conn-status"),
  themeToggle: $("#theme-toggle"),
  // intake
  raw: $("#raw-input"),
  analyze: $("#analyze-btn"),
  sample: $("#sample-btn"),
  // work
  stageIntake: $("#stage-intake"),
  stageWork: $("#stage-work"),
  dims: $("#dims"),
  table: $("#data-table"),
  issueList: $("#issue-list"),
  issueCount: $("#issue-count"),
  opList: $("#op-list"),
  opsEmpty: $("#ops-empty"),
  proposerTag: $("#proposer-tag"),
  appliedList: $("#applied-list"),
  appliedCount: $("#applied-count"),
  undo: $("#undo-btn"),
  export: $("#export-btn"),
  restart: $("#restart-btn"),
};

let anna = null;
let sessionId = null;
let appliedLog = [];
let busy = false;

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

async function init() {
  bindUi();
  honorTheme();
  try {
    anna = await AnnaAppRuntime.connect();
    setConn(true);
    setStatus("Connected");
  } catch (e) {
    setConn(false);
    setStatus("Standalone preview — open inside Anna to clean data", "warn");
    els.analyze.disabled = true;
    console.warn("[tidy-data] standalone:", e?.message || e);
    return;
  }
  // Best-effort: resume a session id from storage so a reload re-hydrates.
  try {
    const r = await anna.storage.get({ key: STORAGE_KEY });
    if (typeof r?.value === "string" && r.value) {
      sessionId = r.value;
      await rehydrate();
    }
  } catch { /* none yet */ }
}

// ---------------------------------------------------------------------------
// Engine RPC
// ---------------------------------------------------------------------------

async function callTidy(action, extra = {}) {
  if (!anna) throw new Error("not connected to Anna");
  const res = await anna.tools.invoke({
    tool_id: TOOL_ID,
    method: TOOL_METHOD,
    args: { action, ...extra },
  });
  // Unwrap InvokeResult { success, data } — tolerate either wrapped or raw.
  const payload = res && typeof res === "object" && "success" in res ? res : { success: true, data: res };
  if (!payload.success) throw new Error(payload.error || "engine error");
  return payload.data;
}

// ---------------------------------------------------------------------------
// Stage 1 — analyze
// ---------------------------------------------------------------------------

async function onAnalyze() {
  if (busy || !anna) return;
  const raw = els.raw.value;
  if (!raw.trim()) { setStatus("Paste some rows first", "warn"); return; }
  setBusy(true); setStatus("Analyzing…");
  try {
    const data = await callTidy("load", { raw_text: raw });
    sessionId = data.session_id;
    appliedLog = [];
    try { await anna.storage.set({ key: STORAGE_KEY, value: sessionId }); } catch {}
    renderPreview(data.preview);
    renderIssues(data.issues);
    els.body.dataset.stage = "work";
    els.stageWork.hidden = false;
    setStatus(`Found ${data.issue_count} issue${data.issue_count === 1 ? "" : "s"}`);
    syncTitle(data.issue_count);
    await proposeFixes(data.issues, data.preview.headers);
  } catch (e) {
    setStatus(`Error: ${e?.message || e}`, "error");
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------------------------
// Proposing fixes — LLM proposes, engine validates+previews. Falls back to the
// engine's deterministic `suggest` when the host LLM is unavailable.
// ---------------------------------------------------------------------------

async function proposeFixes(issues, headers) {
  let ops = null;
  let source = "engine";
  try {
    ops = await llmPropose(issues, headers);
    if (ops && ops.length) source = "AI";
  } catch (e) {
    console.warn("[tidy-data] llm propose failed, using engine suggest:", e?.message || e);
  }
  if (!ops || !ops.length) {
    const s = await callTidy("suggest", { session_id: sessionId });
    ops = s.suggested_ops || [];
    source = "engine";
  }
  // Keep only ops the engine actually understands.
  ops = ops.filter((o) => o && OP_TYPES.has(o.type));
  els.proposerTag.textContent = source === "AI" ? "proposed by AI" : "engine baseline";
  els.proposerTag.dataset.kind = source === "AI" ? "ai" : "engine";
  await renderOps(ops);
}

async function llmPropose(issues, headers) {
  if (!anna?.llm?.complete) return null;
  const issueText = issues.map((i) => "- " + i.label).join("\n") || "- (none detected)";
  const prompt =
`You are a data-cleaning planner for a spreadsheet tool. The deterministic engine
will execute and verify your operations — you only PLAN them.

Columns: ${JSON.stringify(headers)}
Detected issues:
${issueText}

Return ONLY a JSON array of operations, no prose. Each item:
{"type": <one of trim_whitespace|drop_empty_rows|drop_empty_columns|dedupe_rows|normalize_case|standardize_dates|normalize_numbers|fill_blanks|split_column|rename_column>,
 "column": <name, when the op targets a column>,
 "mode": <title|upper|lower, only for normalize_case>,
 "why": <one short human sentence>}
Order them so whitespace/trim comes before dedupe. Only propose fixes justified by
the issues or column names. Max 8 operations.`;
  const resp = await anna.llm.complete({
    messages: [{ role: "user", content: prompt }],
    temperature: 0,
  });
  const text = extractText(resp);
  return parseOps(text);
}

function extractText(resp) {
  if (!resp) return "";
  if (typeof resp === "string") return resp;
  return (
    resp.content ?? resp.text ?? resp.completion ??
    resp.message?.content ??
    (Array.isArray(resp.content) ? resp.content.map((c) => c.text || "").join("") : "") ??
    ""
  );
}

function parseOps(text) {
  if (!text) return null;
  const m = text.match(/\[[\s\S]*\]/);
  if (!m) return null;
  try {
    const arr = JSON.parse(m[0]);
    return Array.isArray(arr) ? arr : null;
  } catch { return null; }
}

// ---------------------------------------------------------------------------
// Rendering — ops review queue
// ---------------------------------------------------------------------------

async function renderOps(ops) {
  els.opList.innerHTML = "";
  els.opsEmpty.hidden = ops.length > 0;
  for (const op of ops) {
    const li = document.createElement("li");
    li.className = "op";
    li.innerHTML = `
      <div class="op__head">
        <span class="op__type">${escapeHtml(prettyType(op))}</span>
        <span class="op__diff muted small">measuring…</span>
      </div>
      <p class="op__why muted small">${escapeHtml(op.why || "")}</p>
      <div class="op__actions">
        <button class="btn btn--primary btn--sm" data-act="approve">Approve</button>
        <button class="btn btn--ghost btn--sm" data-act="skip">Skip</button>
      </div>`;
    els.opList.appendChild(li);

    // Ask the ENGINE for the real effect (never the model).
    let diff = null;
    try {
      const pv = await callTidy("preview", { session_id: sessionId, op });
      diff = pv.diff;
      li.querySelector(".op__diff").textContent = summarizeDiff(diff);
      if (isNoOp(diff)) {
        li.querySelector(".op__diff").textContent = "no change — already clean";
        li.classList.add("op--noop");
      }
    } catch (e) {
      li.querySelector(".op__diff").textContent = "invalid for this table";
      li.classList.add("op--noop");
    }

    li.querySelector('[data-act="approve"]').addEventListener("click", () => approveOp(op, li, diff));
    li.querySelector('[data-act="skip"]').addEventListener("click", () => { li.remove(); checkOpsEmpty(); });
  }
}

async function approveOp(op, li, diff) {
  if (busy) return;
  setBusy(true);
  try {
    const res = await callTidy("apply", { session_id: sessionId, op });
    renderPreview(res.preview);
    appliedLog.push({ op, diff: res.diff });
    renderApplied();
    li.remove();
    checkOpsEmpty();
    // Re-scan: refresh remaining issues after a real change.
    const cur = await callTidy("get", { session_id: sessionId });
    renderIssues(cur.issues);
    syncTitle(cur.issues.length);
    setStatus(summarizeDiff(res.diff));
    if (anna) {
      anna.chat.write_message({
        role: "user",
        content: `Approved fix · ${prettyType(op)} — ${summarizeDiff(res.diff)}`,
      }).catch(() => {});
    }
  } catch (e) {
    setStatus(`Error: ${e?.message || e}`, "error");
  } finally {
    setBusy(false);
  }
}

function renderApplied() {
  els.appliedCount.textContent = String(appliedLog.length);
  els.undo.disabled = appliedLog.length === 0;
  els.appliedList.innerHTML = "";
  for (const a of appliedLog) {
    const li = document.createElement("li");
    li.className = "applied__item";
    li.innerHTML = `<span class="applied__type">${escapeHtml(prettyType(a.op))}</span>
      <span class="muted small">${escapeHtml(summarizeDiff(a.diff))}</span>`;
    els.appliedList.appendChild(li);
  }
}

async function onUndo() {
  if (busy || !appliedLog.length) return;
  setBusy(true);
  try {
    const res = await callTidy("undo", { session_id: sessionId });
    renderPreview(res.preview);
    appliedLog.pop();
    renderApplied();
    const cur = await callTidy("get", { session_id: sessionId });
    renderIssues(cur.issues);
    setStatus("Reverted last fix");
  } catch (e) {
    setStatus(`Error: ${e?.message || e}`, "error");
  } finally { setBusy(false); }
}

async function onExport() {
  if (busy || !sessionId) return;
  setBusy(true);
  try {
    const res = await callTidy("export", { session_id: sessionId });
    // 1) Download the cleaned CSV to the user's device.
    const downloaded = downloadCsv(res.csv, "tidy-data-clean.csv");
    setStatus(`${downloaded ? "Downloaded" : "Exported"} ${res.row_count} rows × ${res.col_count} cols`);
    // 2) Also post it into chat as a backup / shareable artifact.
    if (anna) {
      const body = "```csv\n" + res.csv.trim() + "\n```";
      await anna.chat.write_message({
        role: "user",
        content: `Here is the cleaned dataset (${res.row_count} rows, ${res.col_count} columns, ${appliedLog.length} fixes applied):\n\n${body}`,
      });
    }
  } catch (e) {
    setStatus(`Error: ${e?.message || e}`, "error");
  } finally { setBusy(false); }
}

async function rehydrate() {
  try {
    const cur = await callTidy("get", { session_id: sessionId });
    renderPreview(cur.preview);
    renderIssues(cur.issues);
    appliedLog = (cur.applied || []).map((a) => ({ op: a.op, diff: a.diff }));
    renderApplied();
    els.body.dataset.stage = "work";
    els.stageWork.hidden = false;
    setStatus("Resumed your session");
    await proposeFixes(cur.issues, cur.preview.headers);
  } catch {
    sessionId = null; // stale id; stay on intake
  }
}

function onRestart() {
  sessionId = null;
  appliedLog = [];
  els.opList.innerHTML = "";
  els.appliedList.innerHTML = "";
  els.body.dataset.stage = "intake";
  els.stageWork.hidden = true;
  setStatus("Ready");
  try { anna?.storage.set({ key: STORAGE_KEY, value: "" }); } catch {}
}

// ---------------------------------------------------------------------------
// Rendering — table + issues
// ---------------------------------------------------------------------------

function renderPreview(preview) {
  if (!preview) return;
  els.dims.textContent = `${preview.row_count} rows × ${preview.col_count} cols${preview.truncated ? " · showing first " + preview.rows.length : ""}`;
  const thead = els.table.querySelector("thead");
  const tbody = els.table.querySelector("tbody");
  thead.innerHTML = "<tr>" + preview.headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("") + "</tr>";
  tbody.innerHTML = preview.rows.map((r) =>
    "<tr>" + r.map((c) => `<td>${escapeHtml(String(c))}</td>`).join("") + "</tr>"
  ).join("");
}

function renderIssues(issues) {
  els.issueCount.textContent = String(issues.length);
  els.issueList.innerHTML = "";
  if (!issues.length) {
    const li = document.createElement("li");
    li.className = "issues__clean";
    li.textContent = "✓ No issues detected — your data looks clean.";
    els.issueList.appendChild(li);
    return;
  }
  for (const i of issues) {
    const li = document.createElement("li");
    li.className = "issues__item";
    li.innerHTML = `<span class="issues__dot issues__dot--${escapeHtml(i.kind)}"></span><span>${escapeHtml(i.label)}</span>`;
    els.issueList.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function prettyType(op) {
  const names = {
    trim_whitespace: "Trim whitespace",
    drop_empty_rows: "Drop empty rows",
    drop_empty_columns: "Drop empty columns",
    dedupe_rows: "Remove duplicate rows",
    normalize_case: `Normalize case${op.column ? " · " + op.column : ""}${op.mode ? " (" + op.mode + ")" : ""}`,
    standardize_dates: `Standardize dates${op.column ? " · " + op.column : ""}`,
    normalize_numbers: `Normalize numbers${op.column ? " · " + op.column : ""}`,
    fill_blanks: `Fill blanks${op.column ? " · " + op.column : ""}`,
    split_column: `Split column${op.column ? " · " + op.column : ""}`,
    rename_column: `Rename column${op.column ? " · " + op.column : ""}`,
  };
  return names[op.type] || op.type;
}

function summarizeDiff(d) {
  if (!d) return "";
  const parts = [];
  if (d.rows_removed) parts.push(`${d.rows_removed} row${d.rows_removed === 1 ? "" : "s"} removed`);
  if (d.cols_removed) parts.push(`${d.cols_removed} column${d.cols_removed === 1 ? "" : "s"} removed`);
  if (d.cells_changed) parts.push(`${d.cells_changed} cell${d.cells_changed === 1 ? "" : "s"} changed`);
  if (d.cols_after > d.cols_before) parts.push(`+${d.cols_after - d.cols_before} columns`);
  return parts.length ? parts.join(" · ") : "no change";
}

function isNoOp(d) {
  return d && !d.rows_removed && !d.cols_removed && !d.cells_changed && d.cols_after === d.cols_before;
}

function checkOpsEmpty() {
  els.opsEmpty.hidden = els.opList.children.length > 0;
}

function downloadCsv(csv, filename) {
  // Trigger a real file download. Sandboxed iframes may block this; if so we
  // return false and the chat copy below still delivers the data.
  try {
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    return true;
  } catch (e) {
    console.warn("[tidy-data] download blocked:", e?.message || e);
    return false;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function syncTitle(issueCount) {
  if (!anna) return;
  const t = issueCount > 0 ? `${issueCount} issue${issueCount === 1 ? "" : "s"} — TidyData` : "Clean — TidyData";
  anna.window.set_title({ title: t }).catch(() => {});
}

function setStatus(text, kind) {
  els.status.textContent = text;
  if (kind) els.status.dataset.kind = kind; else delete els.status.dataset.kind;
}
function setBusy(on) { busy = on; els.body.classList.toggle("is-busy", !!on); }
function setConn(on) {
  els.conn.classList.toggle("dot--off", !on);
  els.conn.classList.toggle("dot--on", !!on);
  els.conn.title = on ? "Connected to Anna" : "Disconnected";
}

// theme
const THEME_KEY = "tidydata:theme";
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
}
function effectiveTheme() {
  const e = document.documentElement.getAttribute("data-theme");
  if (e === "light" || e === "dark") return e;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function toggleTheme() {
  const n = effectiveTheme() === "dark" ? "light" : "dark";
  applyTheme(n);
  try { localStorage.setItem(THEME_KEY, n); } catch {}
}
function honorTheme() {
  let s = null;
  try { s = localStorage.getItem(THEME_KEY); } catch {}
  if (s === "light" || s === "dark") applyTheme(s);
}

function bindUi() {
  els.analyze.addEventListener("click", onAnalyze);
  els.sample.addEventListener("click", () => { els.raw.value = SAMPLE; setStatus("Sample loaded — press Analyze"); });
  els.undo.addEventListener("click", onUndo);
  els.export.addEventListener("click", onExport);
  els.restart.addEventListener("click", onRestart);
  els.themeToggle.addEventListener("click", toggleTheme);
}

document.addEventListener("DOMContentLoaded", init);
