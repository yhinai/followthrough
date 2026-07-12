const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
let browserListening = false;
let recognition = null;
let lastActivityId = null;
let toastTimer = null;
let latestActivity = [];
let actionableOnly = false;
const htmlCache = new Map();

// Only touch the DOM when a section's markup actually changed, so the 1.5s poll
// never wipes hover states, text selection, or in-flight CSS animations.
function setHTML(selector, html) {
  if (htmlCache.get(selector) === html) return;
  htmlCache.set(selector, html);
  $(selector).innerHTML = html;
}

// Relative timestamps live outside the cached markup: they are re-stamped every
// tick without forcing a re-render of their section.
const timeEl = (value) => `<time data-ts="${escapeHtml(value ?? "")}"></time>`;
function refreshTimes() {
  document.querySelectorAll("time[data-ts]").forEach((node) => { node.textContent = timeAgo(node.dataset.ts); });
}

function showToast(message, tone = "info") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.dataset.tone = tone;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 3600);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response;
}

async function jsonApi(path, options = {}) { return (await api(path, options)).json(); }
const timeAgo = (value) => {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 5) return "now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return new Date(value).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
};
const label = (value) => String(value || "signal").replaceAll("_", " ");

async function sendSignal(text) {
  if (!text.trim()) return;
  $("#submit").disabled = true;
  $("#submit").textContent = "Archiving and triaging…";
  try {
    const result = await jsonApi("/api/signals", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({text, source:browserListening ? "voice" : "demo", consent:true})});
    if (result.status === "archived") showToast("Archived safely — no action was needed.");
    else showToast("Signal received — Hermes is triaging it now.", "success");
    $("#input").value = "";
    await load();
  } catch (error) { showToast(`Followthrough failed: ${error.message}`, "error"); }
  finally { $("#submit").disabled = false; $("#submit").innerHTML = "Run through Hermes <span>→</span>"; }
}

async function setMode(mode, resumeParked = false) {
  if (mode === "killed" && !window.confirm("Stop listening and every autonomous capability now?")) return;
  await jsonApi("/api/controls/global", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({mode, reason_code:`dashboard_${mode}`, actor:"owner:dashboard", resume_parked:resumeParked})});
  await load();
}

function setBrowserMic(on) {
  browserListening = on;
  $("#listen").innerHTML = `<span class="mic-dot"></span>${on ? "Stop browser mic" : "Browser mic"}`;
  $("#listen").setAttribute("aria-pressed", String(on));
  if (on && ("webkitSpeechRecognition" in window || "SpeechRecognition" in window)) {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognition = new Recognition(); recognition.continuous = true; recognition.interimResults = false;
    recognition.onresult = (event) => sendSignal(event.results[event.results.length - 1][0].transcript);
    recognition.onend = () => { if (browserListening) try { recognition.start(); } catch (_) {} };
    recognition.start();
  } else if (!on && recognition) { recognition.stop(); recognition = null; }
}

function activityCard(item, index) {
  const fresh = index === 0 && item.event_id !== lastActivityId ? " fresh" : "";
  const disposition = item.relevant ? "actionable" : "observed";
  return `<div class="activity-item${fresh}"><div class="source-icon ${escapeHtml(item.source)}">${item.source === "phone" || item.source === "omi" ? "◉" : "↗"}</div><div class="activity-copy"><div><span>${escapeHtml(label(item.classification))}</span>${timeEl(item.received_at)}</div><p>${escapeHtml(item.text)}</p><small class="${disposition}">${item.relevant ? "Promoted to Hermes" : "Archived · no action"}</small></div></div>`;
}

function renderActivity() {
  const visible = actionableOnly ? latestActivity.filter((item) => item.relevant) : latestActivity;
  setHTML("#activity", visible.length
    ? visible.slice(0, 9).map(activityCard).join("")
    : `<div class="empty-state"><span class="empty-orbit"></span><strong>${actionableOnly ? "No actionable signals" : "Listening for a signal"}</strong><small>${actionableOnly ? "Everything is quiet for now." : "Your phone can stay in your pocket."}</small></div>`);
  refreshTimes();
}

function jobRow(job) {
  const state = escapeHtml(job.state || "queued");
  const receipt = job.task_id ? escapeHtml(job.task_id) : "pending";
  return `<div class="job-row"><div class="job-name"><span class="job-icon">${escapeHtml(String(job.category || "?").slice(0,1).toUpperCase())}</span><div><strong>${escapeHtml(job.entity || "Identified signal")}</strong><small>${escapeHtml(label(job.category))}</small></div></div><span class="status ${state}"><i></i>${state}</span><code>${receipt}</code>${timeEl(job.updated_at)}</div>`;
}

function memoryCard(item) {
  return `<div class="memory-item"><span>${escapeHtml(String(item.category || "M").slice(0,1).toUpperCase())}</span><div><strong>${escapeHtml(item.entity)}</strong><small>${escapeHtml(label(item.category))} · ${timeEl(item.created_at)}</small></div></div>`;
}

function renderCurrentJob(jobs, activity) {
  const active = jobs.filter((job) => !["completed", "cancelled", "dead_letter", "failed"].includes(job.state));
  const job = active[0] || jobs[0];
  $("#activeCount").textContent = `${active.length} active`;
  const latestSignal = activity[0];
  const signalIsNewer = latestSignal && (!job || new Date(latestSignal.received_at) > new Date(job.updated_at));
  if (!active.length && signalIsNewer && !latestSignal.relevant) {
    $("#currentJob").className = "current-job observed";
    setHTML("#currentJob", `<div class="focus-symbol quiet">✓</div><span class="focus-state">Latest decision · ${timeEl(latestSignal.received_at)}</span><strong>No action required</strong><p>${escapeHtml(label(latestSignal.classification))} · safely archived</p><div class="progress-track"><i></i></div><small>Hermes was not invoked because the relevance gate found no actionable intent.</small>`);
    return;
  }
  if (!job) return;
  $("#currentJob").className = `current-job ${escapeHtml(job.state)}`;
  setHTML("#currentJob", `<div class="focus-symbol">H</div><span class="focus-state">${active.length ? "Hermes is working" : "Latest completed work"}</span><strong>${escapeHtml(job.entity || "Identified signal")}</strong><p>${escapeHtml(label(job.hermes_status || job.state))}</p><div class="progress-track"><i></i></div><small>${job.task_id ? `Receipt ${escapeHtml(job.task_id)}` : "Creating durable receipt…"}</small>`);
}

async function load() {
  try {
    const [metrics, jobs, controls, memories, activity] = await Promise.all([
      jsonApi("/api/metrics"), jsonApi("/api/jobs"), jsonApi("/api/controls"), jsonApi("/api/memory/operational"), jsonApi("/api/activity")
    ]);
    const mode = controls.global.mode;
    const healthy = metrics.orchestrator?.status === "ok";
    const systemState = $("#systemState");
    if (systemState) {
      setHTML("#systemState", `<span class="pulse"></span> ${healthy ? "Online" : "Needs attention"}`);
      systemState.classList.toggle("healthy", healthy);
    }
    $("#lastSync").textContent = `Updated ${new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"})}`;
    $("#modeLabel").textContent = mode.toUpperCase();
    setHTML("#pause", `<span class="pause-icon"></span>${mode === "running" ? "Pause" : "Resume"}`);
    $("#pause").setAttribute("aria-pressed", String(mode !== "running"));
    document.body.dataset.mode = mode;
    $("#mArchive").textContent = metrics.total ?? 0;
    $("#mJobs").textContent = jobs.length;
    $("#mDone").textContent = metrics.completed ?? 0;
    latestActivity = activity;
    renderActivity();
    if (activity.length) lastActivityId = activity[0].event_id;
    setHTML("#jobs", jobs.length ? jobs.slice(0,12).map(jobRow).join("") : '<div class="empty-state compact"><strong>No delegated work yet</strong><small>Qualified signals will become traceable work here.</small></div>');
    $("#jobSummary").textContent = `Live · ${jobs.filter((job) => job.state === "completed").length} completed · ${jobs.filter((job) => !["completed","cancelled","dead_letter","failed"].includes(job.state)).length} active`;
    renderCurrentJob(jobs, activity);
    $("#memoryCount").textContent = `Live · ${memories.length} items`;
    setHTML("#memories", memories.length ? memories.slice(0,7).map(memoryCard).join("") : '<div class="empty-state compact"><strong>No memory promoted</strong><small>Important entities and preferences will appear here.</small></div>');
    refreshTimes();
  } catch (error) {
    const systemState = $("#systemState");
    if (systemState) systemState.textContent = `Server unreachable · ${error.message}`;
  }
}

$("#sample").onclick = () => { $("#input").value = "Research and safely evaluate https://github.com/pypa/sampleproject"; };
$("#submit").onclick = () => sendSignal($("#input").value);
$("#listen").onclick = () => setBrowserMic(!browserListening);
$("#pause").onclick = () => setMode(document.body.dataset.mode === "running" ? "paused" : "running", true);
$("#feedFilter").onclick = () => {
  actionableOnly = !actionableOnly;
  $("#feedFilter").setAttribute("aria-pressed", String(actionableOnly));
  $("#feedFilter").textContent = actionableOnly ? "Show all signals" : "Show actionable only";
  renderActivity();
};
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    sendSignal($("#input").value);
  } else if (event.key === "/" && document.activeElement !== $("#input")) {
    event.preventDefault();
    const drawer = document.querySelector(".demo-drawer");
    if (drawer) drawer.open = true;
    $("#input").focus();
  } else if (event.key === "Escape" && document.activeElement === $("#input")) {
    $("#input").blur();
  }
});
load();
setInterval(load, 1500);
