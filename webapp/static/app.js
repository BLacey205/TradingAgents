"use strict";

const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* private mode */ } },
};

let current = null; // { ticker, date } of the analysis on screen
let pollTimer = null;

async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  const code = store.get("passcode");
  if (code) headers["X-Passcode"] = code;
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty */ }
  if (res.status === 401) { showLock("Wrong or missing passcode."); throw new Error("locked"); }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function show(el, text) { el.textContent = text || ""; el.hidden = !text; }

// ---------------------------------------------------------------- markdown (safe)

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function inline(s) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
}
function renderMarkdown(text) {
  const lines = (text || "").replace(/\r/g, "").split("\n");
  const out = [];
  let list = null, para = [], table = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`); list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
    const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
    const [head, ...body] = rows;
    out.push(`<div class="tbl"><table><thead><tr>${cells(head).map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${
      body.map((r) => `<tr>${cells(r).map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    table = [];
  };
  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith("|")) { flushPara(); flushList(); table.push(t); continue; }
    flushTable();
    if (!t) { flushPara(); flushList(); continue; }
    const h = t.match(/^#{1,6}\s+(.*)$/);
    if (h) { flushPara(); flushList(); out.push(`<h4>${inline(h[1])}</h4>`); continue; }
    const li = t.match(/^(?:[-*•]|(\d+)[.)])\s+(.*)$/);
    if (li) {
      flushPara();
      const tag = li[1] ? "ol" : "ul";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push(`<li>${inline(li[2])}</li>`);
      continue;
    }
    flushList();
    para.push(t);
  }
  flushPara(); flushList(); flushTable();
  return out.join("");
}

// ---------------------------------------------------------------- views

function showLock(message) {
  $("app").hidden = true;
  $("lock").hidden = false;
  show($("lock-error"), message);
  $("passcode").focus();
}

function ratingClass(r) {
  const x = (r || "").toLowerCase();
  if (x === "buy" || x === "overweight") return "chip buy";
  if (x === "sell" || x === "underweight") return "chip sell";
  return "chip";
}

function formatDate(iso) {
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function renderJob(job) {
  $("job").hidden = false;
  $("job-title").textContent = job.status === "queued"
    ? `${job.ticker}: waiting (${job.queue_position || 1} ahead)`
    : `Analyzing ${job.ticker}`;
  const pct = Math.round((job.progress || 0) * 100);
  $("job-bar").style.width = `${pct}%`;
  $("job-bar").parentElement.setAttribute("aria-valuenow", String(pct));
  $("job-elapsed").textContent = job.elapsed_seconds != null
    ? `${Math.floor(job.elapsed_seconds / 60)}:${String(job.elapsed_seconds % 60).padStart(2, "0")}` : "";
  $("job-stages").innerHTML = job.stages.map((s) =>
    `<li class="${s.done ? "done" : s.active ? "active" : ""}">${esc(s.label)}</li>`).join("");
  show($("job-error"), job.status === "error" ? `It stopped with an error: ${job.error}` : "");
  $("job-note").hidden = job.status === "error";
}

function renderResult(a) {
  current = { ticker: a.ticker, date: a.date };
  $("result").hidden = false;
  $("res-ticker").textContent = a.ticker;
  const mins = a.seconds ? ` · ${Math.round(a.seconds / 60)} min` : "";
  $("res-meta").textContent = `${formatDate(a.date)}${a.model ? " · " + a.model : ""}${mins}`;
  $("res-rating").textContent = a.rating;
  $("res-rating").className = ratingClass(a.rating);
  $("res-confidence").textContent = a.confidence != null ? `${a.confidence}% confident` : "";
  $("res-decision").innerHTML = renderMarkdown(a.decision);
  $("res-reports").innerHTML = (a.reports || []).map((r) =>
    `<details><summary>${esc(r.title)}</summary><div class="md">${renderMarkdown(r.text)}</div></details>`).join("");
  $("qa").innerHTML = "";
  show($("ask-error"), "");
}

async function loadHistory() {
  try {
    const { analyses } = await api("/api/analyses");
    $("history-card").hidden = !analyses.length;
    $("history").innerHTML = analyses.map((a) =>
      `<li><button type="button" data-ticker="${esc(a.ticker)}" data-date="${esc(a.date)}">
         <span><strong>${esc(a.ticker)}</strong> <span class="muted">${esc(formatDate(a.date))}</span></span>
         <span class="${ratingClass(a.rating)}">${esc(a.rating || "")}</span></button></li>`).join("");
  } catch { /* history is optional */ }
}

async function loadTrust() {
  try {
    const t = await api("/api/track-record");
    const a = t.agent_test;
    if (a && a.hit_rate != null) {
      show($("trust"), `How far to trust a rating: when we tested this team on a free AI model, ` +
        `its buy and sell calls were right ${a.right} of ${a.calls} times (${a.hit_rate}%), about a coin flip. ` +
        `Treat a rating as one input, not an answer.`);
    }
  } catch { /* optional */ }
}

async function openAnalysis(ticker, date) {
  const a = await api(`/api/analyses/${encodeURIComponent(ticker)}/${date}`);
  $("job").hidden = true;
  renderResult(a);
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- jobs

function stopPolling() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }

async function poll(jobId) {
  stopPolling();
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status === "done") {
      store.del("job");
      $("job").hidden = true;
      await openAnalysis(job.ticker, job.date);
      loadHistory();
      return;
    }
    renderJob(job);
    if (job.status === "error") { store.del("job"); return; }
    pollTimer = setTimeout(() => poll(jobId), 4000);
  } catch (err) {
    if (err.message === "locked") return;
    store.del("job");
    show($("job-error"), err.message);
  }
}

async function analyze(query, refresh) {
  show($("analyze-error"), "");
  $("analyze-btn").disabled = true;
  try {
    const job = await api("/api/analyze", { query, refresh: !!refresh });
    $("result").hidden = true;
    store.set("job", job.id);
    poll(job.id);
  } catch (err) {
    if (err.message !== "locked") show($("analyze-error"), err.message);
  } finally {
    $("analyze-btn").disabled = false;
  }
}

// ---------------------------------------------------------------- wiring

$("analyze-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("query").value.trim();
  if (q) { $("query").blur(); analyze(q, false); }
});

$("refresh-btn").addEventListener("click", () => { if (current) analyze(current.ticker, true); });

$("ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = $("question").value.trim();
  if (!question || !current) return;
  show($("ask-error"), "");
  $("ask-btn").disabled = true;
  const item = document.createElement("div");
  item.innerHTML = `<p class="q">${esc(question)}</p><p class="a muted">Thinking…</p>`;
  $("qa").appendChild(item);
  $("question").value = "";
  try {
    const { answer } = await api("/api/ask", { ...current, question });
    const a = item.querySelector(".a");
    a.className = "a md";
    a.innerHTML = renderMarkdown(answer);
  } catch (err) {
    item.remove();
    if (err.message !== "locked") show($("ask-error"), err.message);
  } finally {
    $("ask-btn").disabled = false;
  }
});

$("history").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-ticker]");
  if (b) openAnalysis(b.dataset.ticker, b.dataset.date).catch((err) => show($("analyze-error"), err.message));
});

$("lock-form").addEventListener("submit", (e) => {
  e.preventDefault();
  store.set("passcode", $("passcode").value);
  $("passcode").value = "";
  start();
});

async function start() {
  try {
    const health = await api("/api/health");
    if (health.passcode_required && !store.get("passcode")) { showLock(""); return; }
    await api("/api/analyses"); // checks the passcode
  } catch (err) {
    if (err.message !== "locked") showLock(`Can't reach the server: ${err.message}`);
    return;
  }
  $("lock").hidden = true;
  $("app").hidden = false;
  loadTrust();
  loadHistory();
  const pending = store.get("job");
  if (pending) poll(pending);
}

start();
