// High Destination web UI. Plain JS, no build step. Talks to the JSON API under /api.
"use strict";

const API = (window.HD_API_BASE || "") + "/api";
const POLL_BUSY = 5000;
const POLL_IDLE = 30000;
const RATIOS = ["16:9", "21:9", "32:9", "3:2", "16:10", "4:3", "1:1", "9:16", "9:19.5"];
const DEFAULTS = { steps: 28, guidance: 3.5 };

const $ = (id) => document.getElementById(id);
const state = {
  me: null,
  config: null,
  mode: "preset",
  count: 1,
  jobs: new Map(), // job_id -> view from the API
  order: [], // job ids, newest first
  cards: new Map(), // job_id -> element
  next: null,
  worker: null,
  lbIndex: -1,
};

// ---------- helpers ----------
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = body.detail || res.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    throw new Error(msg);
  }
  return body;
}

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem("hd." + key));
    localStorage.setItem("hd." + key, JSON.stringify(value));
  } catch {
    return undefined;
  }
}

let toastTimer;
function toast(msg, ms = 3500) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

const gcd = (a, b) => (b ? gcd(b, a % b) : a);
function ratioLabel(w, h) {
  const g = gcd(w, h);
  const a = w / g, b = h / g;
  if (a <= 32 && b <= 32) return `${a}:${b}`;
  return w >= h ? `${(w / h).toFixed(2)}:1` : `1:${(h / w).toFixed(2)}`;
}

// Job ids start with a UTC timestamp: 20261122-174501-ab12cd
function jobTime(id) {
  const m = /^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)/.exec(id);
  return m ? Date.UTC(m[1], m[2] - 1, m[3], m[4], m[5], m[6]) : Date.now();
}
function ago(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
const isPending = (j) => j.state === "queued" || j.state === "running";

// ---------- size picker ----------
function parseRatio(text) {
  const m = /^\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*$/.exec(text || "");
  if (!m || +m[1] <= 0 || +m[2] <= 0) return null;
  return [+m[1], +m[2]];
}

function currentSize() {
  const long = state.config?.long_side_for_ratio || 3840;
  if (state.mode === "preset") {
    const p = state.config?.presets.find((p) => p.name === $("preset").value);
    return p ? { w: p.width, h: p.height, req: { preset: p.name } } : null;
  }
  if (state.mode === "ratio") {
    const r = parseRatio($("ratio").value);
    if (!r) return null;
    const [a, b] = r;
    const w = a >= b ? long : Math.round((long * a) / b);
    const h = a >= b ? Math.round((long * b) / a) : long;
    return { w, h, req: { ratio: $("ratio").value.trim() } };
  }
  const w = parseInt($("width").value, 10), h = parseInt($("height").value, 10);
  if (!(w >= 64 && w <= 8192 && h >= 64 && h <= 8192)) return null;
  return { w, h, req: { size: `${w}x${h}` } };
}

function renderShape() {
  const s = currentSize();
  const rect = $("shape-rect");
  if (!s) {
    $("shape-px").textContent = "—";
    $("shape-ratio").textContent = "invalid size";
    rect.style.width = rect.style.height = "0";
    $("shape-note").hidden = true;
    return;
  }
  const max = 72, k = max / Math.max(s.w, s.h);
  rect.style.width = `${Math.max(4, s.w * k)}px`;
  rect.style.height = `${Math.max(4, s.h * k)}px`;
  $("shape-px").textContent = `${s.w} × ${s.h}`;
  $("shape-ratio").textContent = ratioLabel(s.w, s.h);
  $("shape-note").hidden = Math.max(s.w, s.h) / Math.min(s.w, s.h) <= 2.5;
  document.querySelectorAll("#ratio-chips .chip").forEach((c) =>
    c.classList.toggle("active", c.dataset.ratio === $("ratio").value.trim()));
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.mode === mode));
  document.querySelectorAll(".mode").forEach((m) => (m.hidden = m.dataset.mode !== mode));
  renderShape();
  saveForm();
}

function setCount(n) {
  state.count = n;
  document.querySelectorAll("#count button").forEach((b) => b.classList.toggle("active", +b.dataset.n === n));
  saveForm();
}

function saveForm() {
  store("form", {
    prompt: $("prompt").value, mode: state.mode, preset: $("preset").value, ratio: $("ratio").value,
    width: $("width").value, height: $("height").value, count: state.count,
    steps: $("steps").value, guidance: $("guidance").value,
  });
}

function restoreForm() {
  const f = store("form");
  if (!f) return;
  $("prompt").value = f.prompt || "";
  if (f.preset && [...$("preset").options].some((o) => o.value === f.preset)) $("preset").value = f.preset;
  if (f.ratio) $("ratio").value = f.ratio;
  if (f.width) $("width").value = f.width;
  if (f.height) $("height").value = f.height;
  if (f.steps) $("steps").value = f.steps;
  if (f.guidance) $("guidance").value = f.guidance;
  syncRanges();
  setCount(f.count || 1);
  setMode(f.mode || "preset");
}

function syncRanges() {
  $("steps-out").textContent = $("steps").value;
  $("guidance-out").textContent = Number($("guidance").value).toFixed(1);
}

function fillFrom(job, { withSeed = true } = {}) {
  $("prompt").value = job.prompt || "";
  const p = state.config.presets.find((p) => p.width === job.width && p.height === job.height);
  if (p) {
    $("preset").value = p.name;
    setMode("preset");
  } else {
    $("width").value = job.width;
    $("height").value = job.height;
    setMode("size");
  }
  $("seed").value = withSeed && job.seed != null ? job.seed : "";
  if (job.steps != null) $("steps").value = job.steps;
  if (job.guidance != null) $("guidance").value = job.guidance;
  syncRanges();
  if (withSeed) document.querySelector(".advanced").open = true;
  saveForm();
}

// ---------- generate ----------
async function generate(e) {
  e?.preventDefault();
  const err = $("form-error");
  err.hidden = true;
  const size = currentSize();
  const prompt = $("prompt").value.trim();
  if (!prompt) return showError("Write a prompt first.");
  if (!size) return showError("The size is not valid.");
  const body = {
    prompt, ...size.req, count: state.count,
    steps: +$("steps").value, guidance: +$("guidance").value,
  };
  if ($("seed").value !== "") body.seed = +$("seed").value;
  const btn = $("submit");
  btn.disabled = true;
  try {
    const res = await api("/jobs", { method: "POST", body: JSON.stringify(body) });
    for (const j of res.jobs.slice().reverse()) addJob(j, true);
    renderGallery();
    toast(res.worker_started ? "Queued. Starting a GPU worker — this takes a few minutes."
      : `Queued ${res.jobs.length} image${res.jobs.length > 1 ? "s" : ""}.`);
    pollSoon();
  } catch (ex) {
    showError(ex.message);
  } finally {
    btn.disabled = false;
  }
  function showError(msg) {
    err.textContent = msg;
    err.hidden = false;
  }
}

// ---------- gallery ----------
function addJob(job, front = false) {
  if (!state.jobs.has(job.job_id)) {
    if (front) state.order.unshift(job.job_id);
    else state.order.push(job.job_id);
  }
  state.jobs.set(job.job_id, job);
}

function pendingText(job) {
  if (job.state === "running") return "Generating…";
  const w = state.worker;
  if (!w) return "Queued";
  if (!w.instance) return w.desired > 0 ? "Requesting a GPU…" : "Waiting for a worker";
  if (w.instance.state.startsWith("Pending")) return "Worker booting…";
  if (w.instance.state === "InService") return "Queued · worker warming up or busy";
  return `Worker ${w.instance.state.toLowerCase()}`;
}

function buildCard(id) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `<div class="frame"></div><div class="status"></div><div class="cap"></div><div class="acts"></div>`;
  state.cards.set(id, el);
  return el;
}

function updateCard(el, job) {
  el.style.setProperty("--r", (job.width && job.height ? job.width / job.height : 16 / 9).toFixed(4));
  el.classList.toggle("pending", isPending(job));
  const frame = el.querySelector(".frame");
  const status = el.querySelector(".status");
  const acts = el.querySelector(".acts");
  el.querySelector(".cap").textContent = job.prompt || "";

  if (job.state === "done") {
    status.hidden = true;
    if (!frame.querySelector("img")) {
      const img = new Image();
      img.alt = job.prompt || "";
      img.loading = "lazy";
      img.decoding = "async";
      img.onload = () => img.classList.add("loaded");
      img.src = job.thumb_url;
      img.onclick = () => openLightbox(job.job_id);
      frame.replaceChildren(img);
      acts.innerHTML = "";
      const dl = Object.assign(document.createElement("a"), { textContent: "↓", title: "Download PNG", href: job.download_url });
      const reuse = Object.assign(document.createElement("button"), { textContent: "Reuse", title: "Reuse settings", type: "button" });
      reuse.onclick = (ev) => { ev.stopPropagation(); fillFrom(state.jobs.get(job.job_id)); toast("Settings copied to the form."); };
      acts.append(reuse, dl);
    } else {
      acts.querySelector("a").href = job.download_url;
    }
    return;
  }
  frame.replaceChildren();
  acts.innerHTML = "";
  status.hidden = false;
  if (job.state === "error") {
    status.innerHTML = `<b>Failed</b><span class="err"></span>`;
    status.querySelector(".err").textContent = job.error || "unknown error";
    const del = Object.assign(document.createElement("button"), { textContent: "✕", title: "Remove", type: "button" });
    del.onclick = () => deleteJob(job.job_id);
    acts.append(del);
    return;
  }
  status.innerHTML = `<b></b><span class="elapsed"></span><span class="px"></span>`;
  status.querySelector("b").textContent = pendingText(job);
  status.querySelector(".elapsed").textContent = ago(Date.now() - jobTime(job.job_id));
  status.querySelector(".px").textContent = `${job.width}×${job.height} · seed ${job.seed}`;
}

function renderGallery() {
  const gallery = $("gallery");
  const els = state.order.map((id) => {
    const el = state.cards.get(id) || buildCard(id);
    updateCard(el, state.jobs.get(id));
    return el;
  });
  // Re-append only if the order changed, so images do not reload.
  const same = els.length === gallery.children.length && els.every((el, i) => gallery.children[i] === el);
  if (!same) gallery.replaceChildren(...els);
  $("empty").hidden = state.order.length > 0;
  $("more").hidden = !state.next;
  const done = state.order.filter((id) => state.jobs.get(id).state === "done").length;
  $("gallery-count").textContent = state.order.length ? `${done} image${done === 1 ? "" : "s"}` : "";
}

async function loadHistory(more = false) {
  try {
    const q = more && state.next ? `?before=${encodeURIComponent(state.next)}` : "";
    const res = await api("/jobs" + q);
    for (const j of res.jobs) addJob(j);
    state.next = res.next;
    renderGallery();
  } catch (ex) {
    toast("Could not load your images: " + ex.message, 6000);
  }
}

async function deleteJob(id) {
  if (!confirm("Delete this image from S3? This cannot be undone.")) return false;
  try {
    await api(`/jobs/${id}`, { method: "DELETE" });
    state.jobs.delete(id);
    state.order = state.order.filter((x) => x !== id);
    state.cards.get(id)?.remove();
    state.cards.delete(id);
    renderGallery();
    return true;
  } catch (ex) {
    toast("Delete failed: " + ex.message);
    return false;
  }
}

// ---------- lightbox ----------
const doneIds = () => state.order.filter((id) => state.jobs.get(id)?.state === "done");

function openLightbox(id) {
  const ids = doneIds();
  state.lbIndex = ids.indexOf(id);
  if (state.lbIndex < 0) return;
  showLightbox();
  if (!$("lightbox").open) $("lightbox").showModal();
}

function showLightbox() {
  const ids = doneIds();
  const job = state.jobs.get(ids[state.lbIndex]);
  if (!job) return $("lightbox").close();
  const img = $("lb-img");
  img.src = job.thumb_url; // instant preview, then the full PNG
  const full = new Image();
  full.onload = () => { if (state.jobs.get(doneIds()[state.lbIndex]) === job) img.src = job.image_url; };
  full.src = job.image_url;
  img.alt = job.prompt;
  $("lb-prompt").textContent = job.prompt;
  const rows = [
    ["Size", `${job.width} × ${job.height} (${ratioLabel(job.width, job.height)})`],
    ["Seed", job.seed],
    ["Steps", job.steps],
    ["Guidance", job.guidance],
    ["Took", job.seconds != null ? `${job.seconds}s` : "—"],
    ["Made", new Date(jobTime(job.job_id)).toLocaleString()],
    ["Job", job.job_id],
  ];
  if (job.ratio_clamped) rows.push(["Note", "edges cropped (wider than 2.5:1)"]);
  $("lb-meta").replaceChildren(...rows.flatMap(([k, v]) => {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = k;
    dd.textContent = v ?? "—";
    return [dt, dd];
  }));
  $("lb-download").href = job.download_url;
  $("lb-prev").hidden = state.lbIndex <= 0;
  $("lb-next").hidden = state.lbIndex >= ids.length - 1;
}

function stepLightbox(d) {
  const n = doneIds().length;
  const i = state.lbIndex + d;
  if (i < 0 || i >= n) return;
  state.lbIndex = i;
  showLightbox();
}
const lbJob = () => state.jobs.get(doneIds()[state.lbIndex]);

// ---------- worker + polling ----------
function renderWorker() {
  const w = state.worker;
  if (!w) return;
  const dot = $("worker-dot"), text = $("worker-text");
  const q = w.queue ? ` · ${w.queue.waiting + w.queue.in_progress} in queue` : "";
  dot.className = "dot";
  if (w.instance) {
    const s = w.instance.state;
    dot.classList.add(s === "InService" ? "ok" : "busy");
    text.textContent = `${w.instance.type} ${s === "InService" ? "running" : s.toLowerCase()}${q}`;
  } else if (w.desired > 0) {
    dot.classList.add("busy");
    text.textContent = `requesting a GPU${q}`;
  } else {
    text.textContent = `worker off (scaled to zero)${q}`;
  }
  $("stop-btn").hidden = !(w.instance || w.desired > 0);
}

let pollTimer;
function pollSoon(ms = 800) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, ms);
}

async function poll() {
  const pending = state.order.filter((id) => isPending(state.jobs.get(id)));
  try {
    const tasks = pending.map((id) => api(`/jobs/${id}`).then((j) => {
      const before = state.jobs.get(id);
      state.jobs.set(id, j);
      if (before && isPending(before) && j.state === "done") toast("Image ready ✔");
      if (before && isPending(before) && j.state === "error") toast("A job failed: " + (j.error || "").slice(0, 120), 6000);
    }).catch(() => {}));
    if (state.me?.is_admin) {
      tasks.push(api("/worker").then((w) => { state.worker = w; renderWorker(); }).catch(() => {}));
    }
    await Promise.all(tasks);
    renderGallery();
  } finally {
    const busy = state.order.some((id) => isPending(state.jobs.get(id))) || state.worker?.instance;
    pollTimer = setTimeout(poll, busy ? POLL_BUSY : POLL_IDLE);
  }
}

// Keep "elapsed" counters on pending cards ticking between polls.
setInterval(() => {
  for (const id of state.order) {
    const j = state.jobs.get(id);
    if (!isPending(j)) continue;
    const el = state.cards.get(id)?.querySelector(".elapsed");
    if (el) el.textContent = ago(Date.now() - jobTime(id));
  }
}, 1000);

// ---------- wiring ----------
function wire() {
  $("gen-form").addEventListener("submit", generate);
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) generate(e);
  });
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => setMode(t.dataset.mode)));
  for (const id of ["preset", "ratio", "width", "height"]) $(id).addEventListener("input", () => { renderShape(); saveForm(); });
  $("prompt").addEventListener("input", saveForm);
  for (const id of ["steps", "guidance"]) $(id).addEventListener("input", () => { syncRanges(); saveForm(); });
  $("seed-clear").onclick = () => ($("seed").value = "");
  $("reset-adv").onclick = () => {
    $("seed").value = "";
    $("steps").value = DEFAULTS.steps;
    $("guidance").value = DEFAULTS.guidance;
    syncRanges();
    saveForm();
  };
  $("ratio-chips").replaceChildren(...RATIOS.map((r) => {
    const c = Object.assign(document.createElement("button"), { type: "button", className: "chip", textContent: r });
    c.dataset.ratio = r;
    c.onclick = () => { $("ratio").value = r; renderShape(); saveForm(); };
    return c;
  }));
  $("count").replaceChildren(...Array.from({ length: Math.min(4, state.config.max_count) }, (_, i) => {
    const b = Object.assign(document.createElement("button"), { type: "button", textContent: String(i + 1) });
    b.dataset.n = i + 1;
    b.onclick = () => setCount(i + 1);
    return b;
  }));
  $("more").onclick = () => loadHistory(true);

  // lightbox
  $("lb-close").onclick = () => $("lightbox").close();
  $("lb-prev").onclick = () => stepLightbox(-1);
  $("lb-next").onclick = () => stepLightbox(1);
  $("lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox" || e.target.classList.contains("lb-img")) $("lightbox").close(); });
  $("lightbox").addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") stepLightbox(-1);
    if (e.key === "ArrowRight") stepLightbox(1);
  });
  $("lb-reuse").onclick = () => { fillFrom(lbJob()); $("lightbox").close(); toast("Settings copied to the form."); };
  $("lb-variation").onclick = () => { fillFrom(lbJob(), { withSeed: false }); $("lightbox").close(); generate(); };
  $("lb-delete").onclick = async () => {
    const id = doneIds()[state.lbIndex];
    if (!(await deleteJob(id))) return;
    const n = doneIds().length;
    if (!n) return $("lightbox").close();
    state.lbIndex = Math.min(state.lbIndex, n - 1);
    showLightbox();
  };

  // worker controls (admin)
  $("stop-btn").onclick = async () => {
    if (!confirm("Stop the GPU worker now? Queued jobs stay in the queue and run next time a worker starts.")) return;
    try {
      await api("/worker/stop", { method: "POST" });
      toast("Stopping the worker.");
      pollSoon();
    } catch (ex) {
      toast("Stop failed: " + ex.message);
    }
  };
  $("logs-btn").onclick = async () => {
    $("logs-text").textContent = "loading…";
    $("logs").showModal();
    try {
      const res = await api("/worker/logs?lines=400");
      $("logs-key").textContent = res.key || "Worker log";
      $("logs-text").textContent = res.text ||
        "No logs yet. A worker uploads its log when it shuts down (use SSM Session Manager on a live one).";
    } catch (ex) {
      $("logs-text").textContent = "Could not load logs: " + ex.message;
    }
  };
  $("logs-close").onclick = () => $("logs").close();
}

async function init() {
  try {
    [state.me, state.config] = await Promise.all([api("/me"), api("/presets")]);
  } catch (ex) {
    document.body.innerHTML = `<p style="padding:24px">Cannot reach the API: ${ex.message}</p>`;
    return;
  }
  $("preset").replaceChildren(...state.config.presets.map((p) => {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = `${p.name} — ${p.width}×${p.height} · ${p.description}`;
    return o;
  }));
  $("preset").value = state.config.default;
  if (!/Mac|iPhone|iPad/.test(navigator.platform)) document.querySelector("#submit kbd").textContent = "Ctrl↵";
  wire();
  setCount(1);
  restoreForm();
  renderShape();
  $("worker").hidden = !state.me.is_admin;
  await loadHistory();
  poll();
}

init();
