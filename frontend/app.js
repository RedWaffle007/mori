// ── config ──────────────────────────────────────────────────────────────────
const API_BASE = "http://localhost:8000";
const POLL_INTERVAL_MS = 4000;

// ── elements ────────────────────────────────────────────────────────────────
const els = {
  resumeSection: document.getElementById("resume-section"),
  resumeList: document.getElementById("resume-list"),
  fileInput: document.getElementById("file-input"),
  runBtn: document.getElementById("run-btn"),
  uploadError: document.getElementById("upload-error"),
  statusSection: document.getElementById("status-section"),
  jobId: document.getElementById("job-id"),
  spinner: document.getElementById("spinner"),
  statusBadge: document.getElementById("status-badge"),
  statusMessage: document.getElementById("status-message"),
  jobError: document.getElementById("job-error"),
  downloadBtn: document.getElementById("download-btn"),
  summaryCard: document.getElementById("summary-card"),
};

let pollTimer = null;

// ── helpers ───────────────────────────────────────────────────────────────────
function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

function setBadge(status) {
  els.statusBadge.textContent = status;
  els.statusBadge.className = "badge " + status;
}

// ── interrupted jobs on load ──────────────────────────────────────────────────
async function loadJobs() {
  try {
    const res = await fetch(`${API_BASE}/api/step1/jobs`);
    if (!res.ok) return;
    const jobs = await res.json();
    const resumable = jobs.filter((j) => j.status === "interrupted");

    els.resumeList.innerHTML = "";
    if (resumable.length === 0) {
      hide(els.resumeSection);
      return;
    }

    resumable.forEach((job) => {
      const card = document.createElement("div");
      card.className = "resume-card";

      const meta = document.createElement("div");
      meta.className = "meta";
      meta.innerHTML = `<div>${job.message}</div><code>${job.job_id}</code>`;

      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "Resume";
      btn.addEventListener("click", () => resumeJob(job.job_id));

      card.appendChild(meta);
      card.appendChild(btn);
      els.resumeList.appendChild(card);
    });
    show(els.resumeSection);
  } catch (err) {
    // backend not reachable — leave resume section hidden
    console.error("Failed to load jobs:", err);
  }
}

// ── run a new job ──────────────────────────────────────────────────────────────
async function runJob() {
  hide(els.uploadError);
  const file = els.fileInput.files[0];
  if (!file) {
    els.uploadError.textContent = "Please choose an .xlsx file first.";
    show(els.uploadError);
    return;
  }

  const form = new FormData();
  form.append("file", file);

  els.runBtn.disabled = true;
  try {
    const res = await fetch(`${API_BASE}/api/step1/run`, {
      method: "POST",
      body: form,
    });
    if (res.status === 409) {
      els.uploadError.textContent = "Another job is already running. Please wait for it to finish.";
      show(els.uploadError);
      return;
    }
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      els.uploadError.textContent = detail.detail || `Upload failed (${res.status}).`;
      show(els.uploadError);
      return;
    }
    const data = await res.json();
    startTracking(data.job_id);
  } catch (err) {
    els.uploadError.textContent = "Could not reach the server.";
    show(els.uploadError);
  } finally {
    els.runBtn.disabled = false;
  }
}

// ── resume an interrupted job ──────────────────────────────────────────────────
async function resumeJob(jobId) {
  try {
    const res = await fetch(`${API_BASE}/api/step1/resume/${jobId}`, { method: "POST" });
    if (res.status === 409) {
      alert("Another job is already running. Please wait for it to finish.");
      return;
    }
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      alert(detail.detail || `Resume failed (${res.status}).`);
      return;
    }
    hide(els.resumeSection);
    startTracking(jobId);
  } catch (err) {
    alert("Could not reach the server.");
  }
}

// ── status tracking ────────────────────────────────────────────────────────────
function startTracking(jobId) {
  if (pollTimer) clearInterval(pollTimer);

  show(els.statusSection);
  els.jobId.textContent = jobId;
  hide(els.jobError);
  hide(els.downloadBtn);
  hide(els.summaryCard);
  els.summaryCard.innerHTML = "";
  setBadge("queued");
  els.statusMessage.textContent = "Queued…";
  show(els.spinner);

  poll(jobId);
  pollTimer = setInterval(() => poll(jobId), POLL_INTERVAL_MS);
}

async function poll(jobId) {
  try {
    const res = await fetch(`${API_BASE}/api/step1/status/${jobId}`);
    if (!res.ok) return;
    const data = await res.json();
    renderStatus(jobId, data);
  } catch (err) {
    // transient network error — keep polling
  }
}

function renderStatus(jobId, data) {
  setBadge(data.status);
  els.statusMessage.textContent = data.message || "";

  if (data.status === "complete") {
    clearInterval(pollTimer);
    hide(els.spinner);
    els.downloadBtn.href = `${API_BASE}/api/step1/download/${jobId}`;
    show(els.downloadBtn);
    renderSummary(data.summary);
  } else if (data.status === "error") {
    clearInterval(pollTimer);
    hide(els.spinner);
    els.jobError.textContent = data.message || "The job failed.";
    show(els.jobError);
  } else {
    // queued / running
    show(els.spinner);
  }
}

// ── summary card ───────────────────────────────────────────────────────────────
function renderSummary(summary) {
  if (!summary) {
    hide(els.summaryCard);
    return;
  }
  const r = summary.results || {};
  const money = (c) => `$${Number(c || 0).toFixed(4)}`;
  const n = (v) => v ?? 0;
  const num = (v) => Number(v || 0).toLocaleString();

  els.summaryCard.innerHTML = `
    <h3>Summary</h3>
    <table class="summary-table">
      <tr><td>Processed</td><td>${n(r.processed)}</td></tr>
      <tr><td>Matched</td><td>${n(r.matched)}</td></tr>
      <tr><td>Updated</td><td>${n(r.updated)}</td></tr>
      <tr><td>Original (kept Excel address)</td><td>${n(r.original)}</td></tr>
      <tr><td>Skipped — non-UK</td><td>${n(r.skipped_non_uk)}</td></tr>
      <tr><td>Skipped — no address</td><td>${n(r.skipped_no_address)}</td></tr>
      <tr><td>No website</td><td>${n(r.no_website)}</td></tr>
    </table>
    <h3>Cost</h3>
    <table class="summary-table">
      <tr><td>Tokens</td><td>${num(summary.total_input_tokens)} in / ${num(summary.total_output_tokens)} out</td></tr>
      <tr class="total-row"><td>Total Cost</td><td>${money(summary.total_cost_usd)}</td></tr>
    </table>
  `;
  show(els.summaryCard);
}

// ── init ────────────────────────────────────────────────────────────────────
els.runBtn.addEventListener("click", runJob);
loadJobs();
