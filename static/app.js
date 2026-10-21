"use strict";

/* ==========================================================================
   Resume Screener UI (simple)
   Routes:  #/dashboard        totals, per-position stats, resumes not in Recruit
            #/                 positions (role folders)
            #/folder/<id>      position or date folder: JD, resumes, screen
            #/candidates       screened candidates
            #/log              activity log
            #/run/<id>         one run
   Resume-derived text is untrusted: everything goes through esc(), and only
   http(s) URLs are rendered as links.
   ========================================================================== */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

const state = {
  status: null, folder: null, selected: new Set(), poll: null,
  cand: { outcome: "", q: "", recruit: "", role: "", sort: "screened", dir: "desc", page: 1, pageSize: 10 },
};

/* ---------------------------------------------------------------- helpers */

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function safeUrl(url) {
  let u = String(url || "").trim();
  if (!u || /^mailto:/i.test(u)) return null;
  if (!/^https?:\/\//i.test(u)) u = "https://" + u;
  try {
    const p = new URL(u);
    return p.protocol === "http:" || p.protocol === "https:" ? p.href : null;
  } catch { return null; }
}

function link(url, label) {
  const safe = safeUrl(url);
  return safe ? `<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(label || url)}</a>` : esc(label || url || "");
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  let resp;
  try { resp = await fetch(path, opts); } catch { throw new Error("Can't reach the server. Is it running?"); }
  let data = {};
  try { data = await resp.json(); } catch { /* non-JSON */ }
  if (!resp.ok) throw new Error(data.error || `Request failed (HTTP ${resp.status})`);
  return data;
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  return d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function secs(ms) {
  if (ms == null) return "";
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

function between(startIso, endIso) {
  if (!startIso || !endIso) return "";
  const ms = new Date(endIso) - new Date(startIso);
  return isNaN(ms) || ms < 0 ? "" : secs(ms);
}

const bytes = n => n == null ? "" : n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`;
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;
const chips = (items, cls = "") => (items || []).length ? `<div class="chips">${items.map(i => `<span class="tag ${cls}">${esc(i)}</span>`).join("")}</div>` : "";

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind === "error" || kind === "success" ? kind : ""}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 4000);
}

const I = {
  folder: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`,
  play: `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5z"/></svg>`,
  warn: `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>`,
  close: `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>`,
  inbox: `<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`,
};

/* ---------------------------------------------------------------- badges */

function scoreBadge(score) {
  if (score == null) return `<span class="muted">—</span>`;
  const s = Math.round(score);
  return `<span class="score ${s >= 70 ? "hi" : s >= 50 ? "mid" : "lo"}">${s}</span>`;
}

function resultTag(s) {
  if (!s) return `<span class="muted">Not screened</span>`;
  if (s.stage && s.stage !== "done") return `<span class="stage"><span class="spinner"></span> Screening…</span>`;
  const tag = { passed: `<span class="tag good">Passed</span>`, rejected: `<span class="tag">Not passed</span>`, failed: `<span class="tag bad">Failed</span>` }[s.outcome] || "";
  const removed = s.workdrive_action === "deleted" ? ` <span class="tag blue">Removed</span>` : "";
  return tag + removed;
}

function fileIcon(name) {
  const ext = String(name).toLowerCase().endsWith(".docx") ? "docx" : "pdf";
  return `<span class="file-icon ${ext}">${ext.toUpperCase()}</span>`;
}

/* ---------------------------------------------------------------- status */

async function loadStatus() {
  try { state.status = await api("/api/status"); } catch (e) { state.status = { error: e.message }; }
  renderStatus();
}

function canDelete() {
  const s = state.status || {};
  return !!s.delete_enabled && ["WorkDrive.files.UPDATE", "WorkDrive.files.DELETE"]
    .every(n => (s.scopes || []).find(x => x.scope === n)?.granted);
}

function renderStatus() {
  const s = state.status;
  const problems = [];
  if (!s || s.error) problems.push("Server unreachable");
  else {
    if (!s.zoho_connected) problems.push("Zoho not connected");
    if (!s.openai_configured) problems.push("OpenAI key missing");
    const missing = (s.scopes || []).filter(x => !x.granted).map(x => x.scope);
    if (missing.length) problems.push(`Zoho permission missing: ${missing.join(", ")}`);
  }
  $("#status").innerHTML = problems.length
    ? problems.map(p => `<div class="sys-row"><span class="dot bad"></span>${esc(p)}</div>`).join("")
    : `<div class="sys-row"><span class="dot ok"></span>Connected</div>`;
  $("#banner").innerHTML = problems.length && s && !s.error
    ? `<div class="banner error">${I.warn}<div><strong>Setup needs attention</strong><p>${problems.map(esc).join(" · ")}</p></div></div>` : "";
}

/* ---------------------------------------------------------------- router */

async function route() {
  closeDrawer();
  const hash = location.hash || "#/";
  const view = $("#view");
  let m;
  const nav = hash.startsWith("#/dashboard") ? "dashboard" : hash.startsWith("#/candidates") ? "candidates"
    : (hash.startsWith("#/log") || hash.startsWith("#/run/")) ? "log" : "screen";
  $$(".nav a").forEach(a => a.classList.toggle("active", a.dataset.nav === nav));
  window.scrollTo(0, 0);

  if (nav === "dashboard") return renderDashboard(view);
  if (nav === "candidates") return renderCandidates(view);
  if ((m = hash.match(/^#\/run\/(\d+)/))) return renderRun(view, Number(m[1]));
  if (nav === "log") return renderLog(view);
  m = hash.match(/^#\/folder\/([A-Za-z0-9]+)/);
  return renderFolder(view, m ? m[1] : null);
}

const loading = view => { view.innerHTML = `<div class="loading-block"><span class="spinner"></span> Loading…</div>`; };
const errorView = (view, msg) => { view.innerHTML = `<div class="card"><div class="empty">${I.warn}<strong>Couldn't load this page</strong>${esc(msg)}</div></div>`; };

function bindRows(root) {
  $$("[data-href]", root).forEach(el => el.addEventListener("click", e => {
    if (!e.target.closest("a, button, input")) location.hash = el.dataset.href;
  }));
  $$("[data-screening]", root).forEach(el => el.addEventListener("click", e => {
    if (!e.target.closest("a, button, input")) openScreening(Number(el.dataset.screening));
  }));
}

/* ---------------------------------------------------------------- folders */

async function renderFolder(view, folderId, { quiet = false } = {}) {
  if (!quiet) loading(view);
  let data;
  try { data = await api(`/api/browse${folderId ? `?folder_id=${encodeURIComponent(folderId)}` : ""}`); }
  catch (e) { return errorView(view, e.message); }

  if (!state.folder || state.folder.folder.id !== data.folder.id) state.selected.clear();
  const ids = new Set(data.files.map(f => f.id));
  state.selected = new Set([...state.selected].filter(id => ids.has(id)));
  state.folder = data;

  view.innerHTML = data.depth === 0 ? rootHtml(data) : folderHtml(data);
  bindFolder(view, data);
  if (data.active_run_id) startPolling(data.active_run_id, data.folder.id);
}

function folderGrid(folders) {
  return `<div class="folder-grid">${folders.map(f => `<a class="folder-card" href="#/folder/${esc(f.id)}">
    <div class="row"><span class="folder-icon">${I.folder}</span>
      <div style="min-width:0"><div class="folder-name">${esc(f.name)}</div>
      <div class="folder-meta">${f.folder_count ? plural(f.folder_count, "folder") : plural(f.resume_count || 0, "resume")}</div></div></div>
  </a>`).join("")}</div>`;
}

function rootHtml(data) {
  return `<div class="page-head"><div><h1>Positions</h1><div class="sub">Choose the position you're hiring for.</div></div>
      <div style="display:flex;gap:8px">
        <button class="btn" id="fetch-emails">Fetch from email</button>
        <button class="btn primary" id="new-folder" data-label="position">+ New position</button>
      </div></div>
    ${data.subfolders.length ? folderGrid(data.subfolders)
      : `<div class="card"><div class="empty">${I.inbox}<strong>No positions yet</strong>Click + New position, or create folders like "Full Stack" in the WorkDrive resumes folder.</div></div>`}`;
}

function folderHtml(data) {
  const jd = data.jd;
  const hasJd = !!(jd && jd.jd_text && jd.jd_text.trim());
  const crumbs = data.path.map((p, i) => {
    const last = i === data.path.length - 1;
    const label = i === 0 ? "Positions" : esc(p.name);
    return (i ? `<span class="sep">/</span>` : "") + (last ? `<span class="current">${label}</span>` : `<a href="${i === 0 ? "#/" : `#/folder/${esc(p.id)}`}">${label}</a>`);
  }).join("");

  return `<nav class="crumbs">${crumbs}</nav>
    <div class="page-head"><div><h1>${esc(data.folder.name)}</h1></div>
      <div style="display:flex;gap:8px">
        ${data.depth === 1 ? `<button class="btn" id="new-folder" data-label="date folder">+ New date folder</button>` : ""}
        <button class="btn" id="upload-btn">Upload resumes</button>
        <input type="file" id="upload-input" accept=".pdf,.docx" multiple hidden>
      </div></div>
    <div class="stack">
      ${jdCardHtml(data, jd, hasJd)}
      <div id="live-run"></div>
      ${data.subfolders.length ? folderGrid(data.subfolders) : ""}
      ${data.files.length ? resumesHtml(data, hasJd) : `<div class="empty">${I.inbox}<strong>No resumes here</strong>Upload .pdf or .docx files to this folder in WorkDrive.</div>`}
    </div>`;
}

function jdCardHtml(data, jd, hasJd) {
  const threshold = jd ? jd.pass_threshold : state.status?.default_threshold ?? 70;
  return `<section class="card">
    <div class="card-head">
      <h2>Job description</h2>
      ${hasJd ? `<div style="display:flex;gap:10px;align-items:center"><span class="muted small">Pass score ${threshold}+</span><button class="btn sm" id="jd-edit">Edit</button></div>` : ""}
    </div>
    <div class="card-body" id="jd-summary" ${hasJd ? "" : "hidden"}><div class="jd-preview">${esc(jd?.jd_text || "")}</div></div>
    <div class="card-body ${hasJd ? "hidden" : ""}" id="jd-editor">
      <div class="field">
        <textarea id="jd-text" placeholder="Paste the job description for ${esc(data.role.name)}…">${esc(jd?.jd_text || "")}</textarea>
      </div>
      <div class="field">
        <label for="jd-threshold">Pass score</label>
        <input type="number" id="jd-threshold" min="0" max="100" value="${threshold}" style="width:90px">
      </div>
      <div class="form-actions">
        ${hasJd ? `<button class="btn ghost" id="jd-cancel">Cancel</button>` : ""}
        <button class="btn primary" id="jd-save">Save</button>
      </div>
    </div>
  </section>`;
}

function resumesHtml(data, hasJd) {
  return `<section class="card">
    <div class="card-head">
      <h2>Resumes <span class="muted" style="font-weight:400">(${data.files.length})</span></h2>
      ${data.files.length ? `<div style="display:flex;gap:8px;align-items:center">
        <button class="btn primary" id="run-btn" disabled>${I.play} Screen selected</button>
        ${data.active_run_id ? `<button class="btn stop-btn" id="stop-btn">${I.stop || "Stop"} Stop screening</button>` : ""}
      </div>` : ""}
    </div>
    ${data.files.length ? `<div class="table-wrap"><table>
      <thead><tr>
        <th class="check"><input type="checkbox" id="select-all" aria-label="Select all"></th>
        <th>File</th><th>Candidate</th><th class="num">Score</th><th>Result</th>
      </tr></thead>
      <tbody id="resume-rows"></tbody></table></div>
      ${hasJd ? "" : `<div class="card-foot muted small">Save a job description to start screening.</div>`}`
      : `<div class="empty">${I.inbox}<strong>No resumes here</strong>Upload .pdf or .docx files to this folder in WorkDrive.</div>`}
  </section>`;
}

function renderResumeRows() {
  const data = state.folder, tbody = $("#resume-rows");
  if (!tbody) return;
  tbody.innerHTML = data.files.map(f => {
    const s = f.last_screening;
    const sel = state.selected.has(f.id);
    const detail = s && s.overall_score != null;
    return `<tr class="${sel ? "selected" : ""} ${detail ? "clickable" : ""}" ${detail ? `data-screening="${s.id}"` : ""}>
      <td class="check"><input type="checkbox" data-file="${esc(f.id)}" ${sel ? "checked" : ""} aria-label="Select ${esc(f.name)}"></td>
      <td><div class="file-cell">${fileIcon(f.name)}<div style="min-width:0">
        <div class="primary-text" title="${esc(f.name)}">${esc(f.name)}</div><div class="secondary-text">${bytes(f.size)}</div></div></div></td>
      <td>${s?.candidate_name ? `<div class="primary-text">${esc(s.candidate_name)}</div><div class="secondary-text">${esc(s.email || "")}</div>` : `<span class="muted">—</span>`}</td>
      <td class="num">${scoreBadge(s?.overall_score)}</td>
      <td>${resultTag(s)}</td>
    </tr>`;
  }).join("");

  const all = $("#select-all");
  all.checked = data.files.length > 0 && state.selected.size === data.files.length;
  all.indeterminate = state.selected.size > 0 && state.selected.size < data.files.length;
  $$("input[data-file]", tbody).forEach(cb => cb.addEventListener("change", () => {
    cb.checked ? state.selected.add(cb.dataset.file) : state.selected.delete(cb.dataset.file);
    renderResumeRows();
  }));
  bindRows(tbody);

  const btn = $("#run-btn"), n = state.selected.size;
  const hasJd = !!(data.jd && data.jd.jd_text);
  btn.disabled = !n || !hasJd || !!state.poll;
  btn.innerHTML = `${I.play} Screen selected${n ? ` (${n})` : ""}`;
  if (state.poll) btn.disabled = true;
}

function bindFolder(view, data) {
  bindRows(view);
  $("#new-folder")?.addEventListener("click", () => newFolderDialog(data));
  $("#fetch-emails")?.addEventListener("click", () => fetchEmails(data));
  if ($("#fetch-emails")) {  // page opened/refreshed while a fetch is already running
    api("/api/fetch-emails/status").then(st => { if (st.running) fetchEmails(data, { attach: true }); }).catch(() => {});
  }
  $("#upload-btn")?.addEventListener("click", () => $("#upload-input").click());
  $("#upload-input")?.addEventListener("change", e => uploadResumes(data, Array.from(e.target.files || [])));
  if (data.depth === 0) return;

  $("#jd-edit")?.addEventListener("click", () => {
    $("#jd-summary").setAttribute("hidden", "");
    $("#jd-editor").classList.remove("hidden");
  });
  $("#jd-cancel")?.addEventListener("click", () => {
    $("#jd-text").value = data.jd?.jd_text || "";
    $("#jd-editor").classList.add("hidden");
    $("#jd-summary").removeAttribute("hidden");
  });
  $("#jd-save").addEventListener("click", async () => {
    const btn = $("#jd-save");
    btn.disabled = true;
    try {
      await api(`/api/roles/${encodeURIComponent(data.role.id)}/jd`, {
        method: "PUT", body: { jd_text: $("#jd-text").value, pass_threshold: Number($("#jd-threshold").value) },
      });
      toast("Job description saved");
      renderFolder($("#view"), data.folder.id, { quiet: true });
    } catch (e) { toast(e.message, "error"); btn.disabled = false; }
  });

  if (!$("#resume-rows")) return;
  $("#select-all").addEventListener("change", e => {
    state.selected = e.target.checked ? new Set(data.files.map(f => f.id)) : new Set();
    renderResumeRows();
  });
  $("#run-btn").addEventListener("click", () => confirmRun(data));
  $("#stop-btn")?.addEventListener("click", () => stopScreening(data.active_run_id));
  renderResumeRows();
}

async function stopScreening(runId) {
  const btn = $("#stop-btn");
  if (!runId || !btn) return;
  btn.disabled = true;
  btn.textContent = "Stopping…";
  try {
    await api(`/api/runs/${runId}`, { method: "DELETE" });
    toast("Stopping screening");
  } catch (e) {
    toast(e.message, "error");
    btn.disabled = false;
    btn.textContent = `${I.stop || "Stop"} Stop screening`;
  }
}

/* ---------------------------------------------------------------- create & upload */

function todayLabel() {
  return new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function newFolderDialog(data) {
  const isDate = data.depth === 1;
  const kind = isDate ? "date folder" : "position";
  openModal(`
    <h2>New ${kind}</h2>
    <p>${isDate ? `Creates a folder inside <b>${esc(data.folder.name)}</b> in WorkDrive.` : "Creates a position folder in WorkDrive."}</p>
    <div class="field"><input type="text" id="folder-name" maxlength="100"
      placeholder="${isDate ? "e.g. 17 Sep" : "e.g. Full Stack"}" value="${isDate ? esc(todayLabel()) : ""}"></div>
    <div class="modal-actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn primary" id="create-folder">Create</button>
    </div>`);
  const input = $("#folder-name");
  input.focus();
  input.select();
  const submit = async () => {
    const name = input.value.trim();
    if (!name) return input.focus();
    $("#create-folder").disabled = true;
    try {
      const folder = await api("/api/folders", { method: "POST", body: { parent_id: data.folder.id, name } });
      closeModal();
      toast(`Created "${folder.name}"`);
      location.hash = `#/folder/${folder.id}`;
    } catch (e) {
      toast(e.message, "error");
      $("#create-folder").disabled = false;
    }
  };
  $("#create-folder").addEventListener("click", submit);
  input.addEventListener("keydown", e => { if (e.key === "Enter") submit(); });
}

let fetchPoll = null;

function fetchLabel(st) {
  if (st.phase === "scanning") return "Reading emails…";
  return st.total ? `Fetching ${st.done}/${st.total}…` : "Fetching…";
}

// Starts (or re-attaches to) the background email fetch and shows live progress.
async function fetchEmails(data, { attach = false } = {}) {
  const btn = $("#fetch-emails");
  if (!btn || fetchPoll) return;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> Reading emails…`;
  const done = () => { clearInterval(fetchPoll); fetchPoll = null; };
  const reset = () => { const b = $("#fetch-emails"); if (b) { b.disabled = false; b.textContent = "Fetch from email"; } };
  let st;
  try {
    st = attach ? await api("/api/fetch-emails/status") : await api("/api/fetch-emails", { method: "POST" });
  } catch (e) { toast(e.message, "error"); return reset(); }
  fetchPoll = true;
  const tick = async () => {
    try { st = await api("/api/fetch-emails/status"); } catch { return; }
    const b = $("#fetch-emails");
    if (b && st.running) b.innerHTML = `<span class="spinner"></span> ${fetchLabel(st)}`;
    if (st.running) return;
    done();
    if (st.error) toast(st.error, "error");
    else if (!st.total) toast("No new application emails.");
    else toast(`Fetched ${st.saved} of ${st.total} resumes${st.failed ? ` · ${st.failed} failed` : ""}`, st.failed ? "error" : "info");
    st.results.filter(r => !r.ok).slice(0, 5).forEach(r => toast(`${r.candidate || r.subject}: ${r.error}`, "error"));
    reset();
    if (location.hash === "#/" || location.hash === "") route();
  };
  fetchPoll = setInterval(tick, 1500);
  tick();
}

async function uploadResumes(data, files) {
  $("#upload-input").value = "";
  if (!files.length) return;
  const btn = $("#upload-btn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> Uploading ${files.length}…`;
  const form = new FormData();
  files.forEach(f => form.append("files", f, f.name));
  try {
    const res = await api(`/api/folders/${encodeURIComponent(data.folder.id)}/upload`, { method: "POST", body: form });
    if (res.uploaded) toast(`Uploaded ${plural(res.uploaded, "resume")}`);
    res.results.filter(r => !r.ok).forEach(r => toast(r.error, "error"));
    await renderFolder($("#view"), data.folder.id, { quiet: true });
    // Tick the new uploads so they're ready to screen.
    res.results.filter(r => r.ok && r.id).forEach(r => state.selected.add(r.id));
    renderResumeRows();
  } catch (e) {
    toast(e.message, "error");
    btn.disabled = false;
    btn.textContent = "Upload resumes";
  }
}

/* ---------------------------------------------------------------- run */

function confirmRun(data) {
  const ids = data.files.filter(f => state.selected.has(f.id)).map(f => f.id);
  if (!ids.length) return;
  const t = data.jd.pass_threshold;
  openModal(`
    <h2>Screen ${plural(ids.length, "resume")}?</h2>
    <p>Candidates scoring <b>${t} or more</b> are saved to Zoho Recruit${canDelete() ? " and <b>permanently removed from WorkDrive</b>" : ""}. Others stay in WorkDrive.</p>
    <div class="modal-actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn primary" id="confirm-run">Start</button>
    </div>`);
  $("#confirm-run").addEventListener("click", async () => {
    $("#confirm-run").disabled = true;
    try {
      const { run_id } = await api("/api/runs", { method: "POST", body: { folder_id: data.folder.id, file_ids: ids } });
      closeModal();
      state.selected.clear();
      startPolling(run_id, data.folder.id);
      renderResumeRows();
    } catch (e) { closeModal(); toast(e.message, "error"); }
  });
}

function startPolling(runId, folderId) {
  if (state.poll && state.poll.runId === runId) return;
  stopPolling();
  state.poll = { runId, folderId, timer: null, announced: new Set(), seeded: false };
  pollOnce();
}

function stopPolling() {
  if (state.poll?.timer) clearTimeout(state.poll.timer);
  state.poll = null;
}

async function pollOnce() {
  const p = state.poll;
  if (!p) return;
  let data;
  try { data = await api(`/api/runs/${p.runId}?after_event=999999999`); }
  catch { if (state.poll === p) p.timer = setTimeout(pollOnce, 4000); return; }
  if (state.poll !== p) return;

  const r = data.run;
  announceFinished(p, data.screenings || []);
  const here = () => state.folder && state.folder.folder.id === p.folderId && $("#live-run");
  if (r.status === "running" || r.status === "cancelling") {
    if (here()) {
      const pct = r.total ? Math.round((r.processed / r.total) * 100) : 0;
      const elapsed = Date.now() - new Date(r.started_at).getTime();
      const left = r.processed && r.total > r.processed ? (elapsed / r.processed) * (r.total - r.processed) : null;
      $("#live-run").innerHTML = `<section class="card run-live"><div class="card-body" style="display:flex;flex-direction:column;gap:10px">
        <div style="display:flex;justify-content:space-between;gap:10px"><b><span class="spinner"></span>&nbsp; ${r.status === "cancelling" ? "Stopping screening" : `Screening ${r.processed} of ${r.total}`}</b>
        <span class="muted">${r.passed} passed · ${secs(elapsed)} elapsed${left ? ` · about ${secs(left)} left` : ""}</span></div>
        <div class="progress"><span style="width:${pct}%"></span></div></div></section>`;
    }
    p.timer = setTimeout(pollOnce, 1500);
    return;
  }
  stopPolling();
  if (r.status === "failed") toast(`Screening failed: ${r.error || "see Activity log"}`, "error");
  if (r.status === "cancelled") toast("Screening stopped", "error");
  showRunResults(r, data.screenings || []);
  if (here()) {
    await renderFolder($("#view"), p.folderId, { quiet: true });
    $("#live-run").innerHTML = "";
  }
}

/* One toast per resume as it finishes. Resumes already done when polling
   starts (e.g. page reloaded mid-run) are marked seen without a toast. */
function announceFinished(p, screenings) {
  for (const s of screenings) {
    if (s.stage !== "done" || p.announced.has(s.id)) continue;
    p.announced.add(s.id);
    if (!p.seeded || s.outcome !== "passed") continue;
    const who = `${s.candidate_name || s.filename} passed (${Math.round(s.overall_score)})`;
    if (s.recruit_status === "saved") toast(s.recruit_action === "update"
      ? `✓ ${who} - already in Zoho Recruit, their record was updated`
      : `✓ ${who} and was added to Zoho Recruit as a new candidate`, "success");
    else toast(`${who} but was NOT saved to Zoho Recruit - ${s.recruit_error || "see Activity log"}`, "error");
  }
  p.seeded = true;
}

function resultRowHtml(s) {
  const tags = [];
  let note = "";
  if (s.outcome === "failed") { tags.push(`<span class="tag bad">Failed</span>`); note = s.error; }
  else if (s.outcome === "rejected") tags.push(`<span class="tag">Not passed</span>`);
  else if (s.outcome === "passed") {
    tags.push(`<span class="tag good">Passed</span>`);
    if (s.recruit_status === "saved") tags.push(s.recruit_action === "update"
      ? `<span class="tag good" title="A candidate with this email was already in Recruit - their record was updated">Updated existing candidate in Recruit</span>`
      : `<span class="tag good">New candidate in Recruit</span>`);
    else { tags.push(`<span class="tag bad">Not saved to Recruit</span>`); note = s.recruit_error; }
    if (s.workdrive_action === "deleted") tags.push(`<span class="tag blue">Removed from WorkDrive</span>`);
    else if (s.recruit_status === "saved") tags.push(`<span class="tag warn" title="${esc(s.workdrive_reason || "")}">Still in WorkDrive</span>`);
  }
  const open = s.overall_score != null;
  return `<li class="${open ? "clickable" : ""}" ${open ? `data-result="${s.id}"` : ""}>
    ${scoreBadge(s.overall_score)}
    <div class="rr-main"><div class="primary-text">${esc(s.candidate_name || s.filename)}</div>
      <div class="secondary-text">${esc(s.candidate_name ? s.filename : s.email || "")}</div>
      <div class="chips">${tags.join("")}</div>
      ${note ? `<div class="rr-note">${esc(note)}</div>` : ""}</div></li>`;
}

function showRunResults(r, screenings) {
  const saved = screenings.filter(s => s.outcome === "passed" && s.recruit_status === "saved").length;
  const unsaved = r.passed - saved;
  const title = ["failed", "cancelled"].includes(r.status) ? "Screening stopped"
    : r.passed ? `${plural(r.passed, "candidate")} passed` : "No candidates passed";
  const summary = [
    r.passed ? `${saved} saved to Zoho Recruit${unsaved ? `, <b>${unsaved} not saved</b>` : ""}` : "",
    r.rejected ? `${r.rejected} below the pass score (${r.pass_threshold})` : "",
    r.failed ? `${r.failed} could not be screened` : "",
    between(r.started_at, r.finished_at) ? `took ${between(r.started_at, r.finished_at)}` : "",
  ].filter(Boolean).join(" · ");
  const order = { passed: 0, failed: 1, rejected: 2 };
  const rows = [...screenings].sort((a, b) => (order[a.outcome] ?? 3) - (order[b.outcome] ?? 3) || (b.overall_score ?? -1) - (a.overall_score ?? -1));
  openModal(`
    <h2>${r.passed && !unsaved ? "✓ " : ""}${esc(title)}</h2>
    <p>${summary}</p>
    <ul class="run-results">${rows.map(resultRowHtml).join("")}</ul>
    <div class="modal-actions">
      <a class="btn" href="#/run/${r.id}" data-close>Activity log</a>
      <button class="btn primary" data-close>OK</button>
    </div>`);
  $$("#modal [data-result]").forEach(el => el.addEventListener("click", () => {
    closeModal();
    openScreening(Number(el.dataset.result));
  }));
}

/* ---------------------------------------------------------------- dashboard */

function tile(label, value, note, cls) {
  return `<div class="tile"><div class="tile-label">${esc(label)}</div>
    <div class="tile-value ${cls || ""}">${esc(String(value))}</div>
    <div class="tile-note">${note || ""}</div></div>`;
}

async function renderDashboard(view) {
  loading(view);
  let d;
  try { d = await api("/api/dashboard"); } catch (e) { return errorView(view, e.message); }
  const t = d.totals;
  const notSaved = Math.max(0, t.passed - t.in_recruit);
  const pct = (n, of) => of ? `${Math.round((n / of) * 100)}% of screened` : "";

  view.innerHTML = `
    <div class="page-head"><div><h1>Dashboard</h1>
      <div class="sub">Every resume the screener has parsed, counted once per file.</div></div></div>
    <div class="stack">
      <div class="tiles">
        ${tile("Resumes parsed", t.screened, "across all positions")}
        ${tile("Passed", t.passed, pct(t.passed, t.screened), "good")}
        ${tile("Not passed", t.rejected, pct(t.rejected, t.screened))}
        ${tile("Could not be read", t.failed, t.failed ? "scanned PDFs or download errors" : "", t.failed ? "bad" : "")}
        ${tile("Saved to Zoho Recruit", t.in_recruit, t.passed ? `of ${plural(t.passed, "passed resume")}` : "", "good")}
        ${tile("Passed, not in Recruit", notSaved, notSaved ? "see the list below" : "nothing pending", notSaved ? "bad" : "")}
        ${tile("Average score", t.avg_score ?? "—", `pass marks are set per position`)}
        ${tile("Average time per resume", t.avg_seconds ? `${t.avg_seconds}s` : "—", t.total_seconds ? `${secs(t.total_seconds * 1000)} of screening time` : "measured from this version on")}
      </div>

      <section class="card"><div class="card-head"><h2>By position</h2></div>
        ${d.roles.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Position</th><th class="num">Parsed</th><th class="num">Passed</th><th class="num">In Recruit</th>
            <th class="num">Avg score</th><th>Pass rate</th><th>Last activity</th></tr></thead>
          <tbody>${d.roles.map(r => {
            const rate = r.screened ? Math.round((r.passed / r.screened) * 100) : 0;
            return `<tr><td><b>${esc(r.role_name)}</b></td>
              <td class="num">${r.screened}</td><td class="num">${r.passed}</td><td class="num">${r.in_recruit}</td>
              <td class="num">${r.avg_score ?? "—"}</td>
              <td><div class="bar" title="${rate}% passed"><span style="width:${rate}%"></span></div>
                <span class="muted small">${rate}%</span></td>
              <td class="muted small nowrap">${when(r.last_activity)}</td></tr>`;
          }).join("")}</tbody></table></div>` : `<div class="empty">Nothing screened yet.</div>`}
      </section>

      <section class="card"><div class="card-head"><h2>Passed but not in Zoho Recruit</h2></div>
        ${d.not_in_recruit.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Candidate</th><th>Position</th><th class="num">Score</th><th>Why not</th><th>Screened</th></tr></thead>
          <tbody>${d.not_in_recruit.map(c => `<tr class="clickable" data-screening="${c.id}">
            <td><div class="primary-text">${esc(c.candidate_name || c.filename)}</div><div class="secondary-text">${esc(c.email || c.filename)}</div></td>
            <td>${esc(c.role_name)}</td><td class="num">${scoreBadge(c.overall_score)}</td>
            <td class="wrap">${esc(c.recruit_error || "The Recruit push did not finish - see the Activity log")}</td>
            <td class="muted small nowrap">${when(c.updated_at)}</td></tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">${I.inbox}<strong>Every passed resume reached Recruit</strong></div>`}
      </section>

      <section class="card"><div class="card-head"><h2>Recent runs</h2></div>
        ${d.recent_runs.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Folder</th><th>Started</th><th class="num">Resumes</th><th class="num">Passed</th><th class="num">Took</th><th>Status</th></tr></thead>
          <tbody>${d.recent_runs.map(r => `<tr class="clickable" data-href="#/run/${r.id}">
            <td><b>${esc(r.folder_path.split(" / ").slice(1).join(" / "))}</b></td>
            <td class="muted nowrap">${when(r.started_at)}</td>
            <td class="num">${r.total}</td><td class="num">${r.passed}</td>
            <td class="num muted small nowrap">${esc(between(r.started_at, r.finished_at))}</td>
            <td>${runStatusTag(r)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No runs yet.</div>`}
      </section>
    </div>`;
  bindRows(view);
}

function runStatusTag(r) {
  if (r.status === "running") return `<span class="tag blue">Running</span>`;
  if (r.status === "failed") return `<span class="tag bad">Failed</span>`;
  return `<span class="tag good">Done</span>`;
}

/* ---------------------------------------------------------------- candidates */

async function renderCandidates(view) {
  const seg = (name, options) => `<div class="segmented" role="group">${options.map(([k, l]) =>
    `<button data-${name}="${k}" class="${state.cand[name] === k ? "on" : ""}">${l}</button>`).join("")}</div>`;
  view.innerHTML = `
    <div class="page-head"><div><h1>Candidates</h1><div class="sub" id="cand-count"></div></div></div>
    <section class="card">
      <div class="toolbar">
        ${seg("outcome", [["", "All"], ["passed", "Passed"], ["rejected", "Not passed"], ["failed", "Failed"]])}
        ${seg("recruit", [["", "Any Recruit status"], ["saved", "In Recruit"], ["not_saved", "Passed, not in Recruit"]])}
        <select id="cand-role" aria-label="Position"><option value="">All positions</option></select>
        <span class="spacer"></span>
        <label class="muted small" for="cand-page-size">Rows</label>
        <select id="cand-page-size" aria-label="Rows per page">
          ${[10, 20, 30].map(size => `<option value="${size}" ${state.cand.pageSize === size ? "selected" : ""}>${size}</option>`).join("")}
        </select>
        <input type="search" id="cand-q" placeholder="Search name, email or file" value="${esc(state.cand.q)}">
      </div>
      <div id="cand-body"><div class="loading-block"><span class="spinner"></span> Loading…</div></div>
    </section>`;
  for (const name of ["outcome", "recruit"]) {
    $$(`[data-${name}]`).forEach(b => b.addEventListener("click", () => {
      state.cand[name] = b.dataset[name];
      state.cand.page = 1;
      $$(`[data-${name}]`).forEach(x => x.classList.toggle("on", x === b));
      loadCandidates();
    }));
  }
  $("#cand-role").addEventListener("change", e => { state.cand.role = e.target.value; state.cand.page = 1; loadCandidates(); });
  $("#cand-page-size").addEventListener("change", e => {
    state.cand.pageSize = Number(e.target.value) || 10;
    state.cand.page = 1;
    loadCandidates();
  });
  let timer;
  $("#cand-q").addEventListener("input", e => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.cand.q = e.target.value.trim(); state.cand.page = 1; loadCandidates(); }, 250);
  });
  loadCandidates();
}

/* Sorting is client side: the API already returns the newest 500 rows. */
const CAND_SORTS = {
  name: c => (c.candidate_name || c.filename || "").toLowerCase(),
  position: c => (c.folder_path || "").toLowerCase(),
  score: c => c.overall_score ?? -1,
  recruit: c => c.recruit_status || "",
  time: c => c.duration_ms ?? -1,
  screened: c => c.updated_at || "",
};

function sortCandidates(rows) {
  const key = CAND_SORTS[state.cand.sort] || CAND_SORTS.screened;
  const dir = state.cand.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = key(a), y = key(b);
    return (x < y ? -1 : x > y ? 1 : 0) * dir;
  });
}

function sortableHead(cols) {
  return `<thead><tr>${cols.map(([key, label, cls]) => {
    const on = state.cand.sort === key;
    return `<th class="${cls || ""} sortable ${on ? "sorted" : ""}" data-sort="${key}">${label}` +
      `<span class="arrow">${on ? (state.cand.dir === "asc" ? "▲" : "▼") : ""}</span></th>`;
  }).join("")}</tr></thead>`;
}

function recruitCell(c) {
  if (c.recruit_status === "saved") {
    return `<span class="tag good" title="${c.recruit_action === "update" ? "Existing candidate updated" : "Added as a new candidate"}">${c.recruit_action === "update" ? "Updated" : "Saved"}</span>`;
  }
  if (c.recruit_status === "failed") return `<span class="tag bad" title="${esc(c.recruit_error || "")}">Failed</span>`;
  if (c.outcome === "passed") return `<span class="tag warn" title="${esc(c.workdrive_reason || "The Recruit push did not complete")}">Not saved</span>`;
  return `<span class="muted">—</span>`;
}

async function loadCandidates() {
  const body = $("#cand-body");
  if (!body) return;
  const qs = new URLSearchParams();
  for (const [key, param] of [["outcome", "outcome"], ["q", "q"], ["recruit", "recruit"], ["role", "role"]]) {
    if (state.cand[key]) qs.set(param, state.cand[key]);
  }
  let data;
  try { data = await api(`/api/candidates?${qs}`); } catch (e) { body.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }

  const select = $("#cand-role");
  if (select && select.options.length <= 1) {
    select.innerHTML = `<option value="">All positions</option>` +
      data.roles.map(r => `<option value="${esc(r.id)}" ${state.cand.role === r.id ? "selected" : ""}>${esc(r.name)}</option>`).join("");
  }
  const count = $("#cand-count");
  if (count) count.textContent = `${plural(data.candidates.length, "resume")} screened${data.candidates.length === 500 ? " (newest 500)" : ""}`;

  const sorted = sortCandidates(data.candidates);
  const total = sorted.length;
  const pages = Math.max(1, Math.ceil(total / state.cand.pageSize));
  state.cand.page = Math.min(state.cand.page, pages);
  const start = (state.cand.page - 1) * state.cand.pageSize;
  const rows = sorted.slice(start, start + state.cand.pageSize);
  const range = total ? `${start + 1}-${Math.min(start + state.cand.pageSize, total)} of ${total}` : "0 of 0";
  body.innerHTML = data.candidates.length ? `<div class="table-wrap"><table>
    ${sortableHead([["name", "Candidate"], ["position", "Position"], ["score", "Score", "num"],
                    ["", "Result"], ["recruit", "Recruit"], ["time", "Took", "num"], ["screened", "Screened"]])}
    <tbody>${rows.map(c => `<tr class="${c.overall_score != null ? "clickable" : ""}" ${c.overall_score != null ? `data-screening="${c.id}"` : ""}>
      <td><div class="primary-text">${esc(c.candidate_name || c.filename)}</div><div class="secondary-text">${esc(c.email || c.filename)}</div></td>
      <td>${esc(c.folder_path.split(" / ").slice(1).join(" / "))}</td>
      <td class="num">${scoreBadge(c.overall_score)}</td>
      <td>${resultTag({ ...c, stage: "done" })}</td>
      <td>${recruitCell(c)}</td>
      <td class="num muted small nowrap">${esc(secs(c.duration_ms))}</td>
      <td class="muted small nowrap" title="${esc(c.updated_at || "")}">${when(c.updated_at)}</td>
    </tr>`).join("")}</tbody></table></div>
    <div class="pagination" aria-label="Candidates pagination">
      <span class="muted small">Showing ${range}</span>
      <span class="spacer"></span>
      <button class="btn sm" data-page="prev" ${state.cand.page <= 1 ? "disabled" : ""}>Previous</button>
      <span class="small">Page ${state.cand.page} of ${pages}</span>
      <button class="btn sm" data-page="next" ${state.cand.page >= pages ? "disabled" : ""}>Next</button>
    </div>`
    : `<div class="empty">${I.inbox}<strong>No candidates match these filters</strong></div>`;
  $$("[data-sort]", body).forEach(th => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (!key) return;
    state.cand.dir = state.cand.sort === key && state.cand.dir === "desc" ? "asc" : "desc";
    state.cand.sort = key;
    state.cand.page = 1;
    loadCandidates();
  }));
  $("[data-page=prev]", body)?.addEventListener("click", () => { state.cand.page--; loadCandidates(); });
  $("[data-page=next]", body)?.addEventListener("click", () => { state.cand.page++; loadCandidates(); });
  bindRows(body);
}

/* ---------------------------------------------------------------- log & run */

function logHtml(events) {
  if (!events.length) return `<div class="empty" style="padding:18px">No activity yet.</div>`;
  return `<div class="log">${events.map(e => `<div class="log-line ${esc(e.level)}">
    <span class="t">${esc(new Date(e.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))}</span>
    <span class="lv ${esc(e.level)}">${esc(e.level === "delete" ? "removed" : e.level)}</span>
    <span class="m">${esc(e.message)}</span></div>`).join("")}</div>`;
}

async function renderLog(view) {
  loading(view);
  let runs, events;
  try { [runs, events] = await Promise.all([api("/api/runs?limit=50"), api("/api/events?limit=300")]); }
  catch (e) { return errorView(view, e.message); }

  view.innerHTML = `
    <div class="page-head"><div><h1>Activity log</h1></div></div>
    <div class="stack">
      <section class="card"><div class="card-head"><h2>Screening runs</h2></div>
        ${runs.runs.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Folder</th><th>When</th><th class="num">Screened</th><th class="num">Passed</th><th class="num">Removed</th><th class="num">Took</th><th>Status</th></tr></thead>
          <tbody>${runs.runs.map(r => `<tr class="clickable" data-href="#/run/${r.id}">
            <td><b>${esc(r.folder_path.split(" / ").slice(1).join(" / "))}</b></td>
            <td class="muted nowrap">${when(r.started_at)}</td>
            <td class="num">${r.total}</td><td class="num">${r.passed}</td><td class="num">${r.deleted}</td>
            <td class="num muted small nowrap">${esc(between(r.started_at, r.finished_at))}</td>
            <td>${runStatusTag(r)}</td>
          </tr>`).join("")}</tbody></table></div>` : `<div class="empty">No runs yet.</div>`}
      </section>
      <section class="card"><div class="card-head"><h2>Events</h2></div>${logHtml(events.events.slice().reverse())}</section>
    </div>`;
  bindRows(view);
}

async function renderRun(view, runId) {
  loading(view);
  let data;
  try { data = await api(`/api/runs/${runId}`); } catch (e) { return errorView(view, e.message); }
  const r = data.run;
  const name = esc(r.folder_path.split(" / ").slice(1).join(" / "));
  view.innerHTML = `
    <nav class="crumbs"><a href="#/log">Activity log</a><span class="sep">/</span><span class="current">${name}</span></nav>
    <div class="page-head"><div><h1>${name}</h1>
      <div class="sub">${when(r.started_at)}${between(r.started_at, r.finished_at) ? ` · took ${between(r.started_at, r.finished_at)}` : ""} · ${r.passed} passed · ${r.rejected} not passed${r.failed ? ` · ${r.failed} failed` : ""} · ${r.deleted} removed</div></div></div>
    <div class="stack">
      ${r.error ? `<div class="callout">${I.warn}<div>${esc(r.error)}</div></div>` : ""}
      <section class="card">
        ${data.screenings.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Candidate</th><th class="num">Score</th><th>Result</th><th>Recruit</th><th class="num">Took</th></tr></thead>
          <tbody>${data.screenings.map(s => `<tr class="${s.overall_score != null ? "clickable" : ""}" ${s.overall_score != null ? `data-screening="${s.id}"` : ""}>
            <td><div class="primary-text">${esc(s.candidate_name || s.filename)}</div><div class="secondary-text">${esc(s.error || s.filename)}</div></td>
            <td class="num">${scoreBadge(s.overall_score)}</td><td>${resultTag(s)}</td>
            <td>${recruitCell(s)}</td>
            <td class="num muted small nowrap">${esc(secs(s.duration_ms))}</td></tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">No resumes in this run.</div>`}
      </section>
      <section class="card"><div class="card-head"><h2>Log</h2></div>${logHtml(data.events)}</section>
    </div>`;
  bindRows(view);
}

/* ---------------------------------------------------------------- candidate drawer */

async function openScreening(id) {
  $("#drawer-body").innerHTML = `<div class="loading-block"><span class="spinner"></span> Loading…</div>`;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  try { showCandidate(await api(`/api/screenings/${id}`)); }
  catch (e) { $("#drawer-body").innerHTML = `<div class="d-body"><div class="callout">${I.warn}<div>${esc(e.message)}</div></div></div>`; }
}

function showCandidate(s) {
  $("#drawer-body").innerHTML = candidateHtml(s);
  $$("#drawer-body [data-close]").forEach(el => el.addEventListener("click", closeDrawer));
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
}

function basicInfo(j) {
  if (j.basic_information) return j.basic_information;
  // Screenings saved before the basic-information format.
  const c = j.candidate || {}, prof = j.professional || {}, loc = j.location || {}, links = j.links || {};
  const [first, ...rest] = String(c.full_name || "").split(" ");
  return {
    first_name: c.first_name || first || "", last_name: c.last_name || rest.join(" "),
    contact: c.phone, email: c.email, phone: c.alternate_phone,
    location: loc.current_location || [loc.city, loc.state, loc.country].filter(Boolean).join(", "),
    linkedin_url: links.linkedin, experience_years: prof.total_experience_years,
    experience: [prof.current_job_title, prof.current_employer].filter(Boolean).join(" at "),
    portfolio_url: links.portfolio || links.website, project_urls: links.live_project_links || [],
    education_board: "", position_applied: "",
  };
}

function candidateHtml(s) {
  const j = s.ai_json || {};
  const b = basicInfo(j), ev = j.evaluation || {};
  const name = [b.first_name, b.last_name].filter(Boolean).join(" ") || s.candidate_name || s.filename;

  const tags = [];
  if (!s.error) {
    tags.push(resultTag({ ...s, stage: "done" }));
    if (s.recruit_status === "saved") tags.push(`<span class="tag good">Saved to Recruit</span>`);
    if (s.recruit_status === "failed") tags.push(`<span class="tag bad" title="${esc(s.recruit_error || "")}">Recruit save failed</span>`);
  }

  const urlCell = u => safeUrl(u) ? link(u) : "";
  const rows = [
    ["Salutation", esc(b.salutation)],
    ["First name", esc(b.first_name)],
    ["Last name", esc(b.last_name)],
    ["Gender", esc(b.gender)],
    ["Age (yrs)", esc(b.age_years ?? "")],
    ["Marital status", esc(b.marital_status)],
    ["Contact", esc(b.contact)],
    ["Phone", esc(b.phone)],
    ["Email", esc(b.email)],
    ["Secondary email", esc(b.secondary_email)],
    ["Address", esc([b.street, b.city, b.state, b.zip_code, b.country].filter(Boolean).join(", "))],
    ["Current location", esc(b.location)],
    ["Hometown location", esc(b.hometown_location)],
    ["LinkedIn URL", urlCell(b.linkedin_url)],
    ["Experience", esc(b.work_history ? b.experience
      : [b.experience_years != null ? `${b.experience_years} yrs` : "", b.experience].filter(Boolean).join(" · "))],
    ["Current job title", esc(b.current_job_title)],
    ["Portfolio / Project URL", [b.portfolio_url, ...(b.project_urls || [])].map(urlCell).filter(Boolean).join("<br>")],
    ["Highest qualification", esc(b.highest_qualification)],
    ["Year of clearing B.Tech / BCA / MCA", esc([b.graduation_year, b.tech_degree && `(${b.tech_degree})`].filter(Boolean).join(" "))],
    ["Education board", esc(b.education_board)],
    ["Skill set", esc((b.skills || []).join(", "))],
    ["Department", esc(b.department)],
    ["Position applied", esc(b.position_applied)],
  ];

  return `<div class="d-head"><div class="d-head-top">
      <div style="min-width:0"><div class="d-title" id="drawer-title">${esc(name)}</div>
        <div class="d-sub">${esc(s.filename)}</div></div>
      <button class="btn ghost" data-close aria-label="Close">${I.close}</button></div></div>
    <div class="d-body">
      ${s.error ? `<div class="callout">${I.warn}<div>${esc(s.error)}</div></div>` : ""}
      <section class="card"><div class="card-body" style="display:flex;flex-direction:column;gap:12px">
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
          <span class="score ${s.overall_score >= 70 ? "hi" : s.overall_score >= 50 ? "mid" : "lo"}" style="font-size:20px;height:40px;min-width:54px">${s.overall_score ?? "–"}</span>
          <div class="chips">${tags.join("")}</div>
        </div>
        ${ev.summary ? `<p style="margin:0;color:var(--text-2)">${esc(ev.summary)}</p>` : ""}
        ${(ev.matched_skills || []).length ? `<div><div class="muted small" style="margin-bottom:5px">Matches the job</div>${chips(ev.matched_skills, "good")}</div>` : ""}
        ${(ev.missing_skills || []).length ? `<div><div class="muted small" style="margin-bottom:5px">Missing</div>${chips(ev.missing_skills, "bad")}</div>` : ""}
      </div></section>
      <section class="card"><div class="card-head"><h3>Basic information</h3></div><div class="card-body">
        <dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v || `<span class="muted">—</span>`}</dd>`).join("")}</dl>
      </div></section>
    </div>`;
}

/* ---------------------------------------------------------------- modal & boot */

function openModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal").classList.add("open");
  $("#modal").setAttribute("aria-hidden", "false");
  $$("#modal [data-close]").forEach(el => el.addEventListener("click", closeModal));
}

function closeModal() {
  $("#modal").classList.remove("open");
  $("#modal").setAttribute("aria-hidden", "true");
}

document.addEventListener("keydown", e => { if (e.key === "Escape") { closeModal(); closeDrawer(); } });
$$("#drawer .drawer-scrim").forEach(el => el.addEventListener("click", closeDrawer));
$$("#modal .modal-scrim").forEach(el => el.addEventListener("click", closeModal));
window.addEventListener("hashchange", route);

loadStatus();
route();
setInterval(loadStatus, 60000);
