const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
let browserListening = false;
let recognition = null;
let lastActivityId = null;

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
    if (result.status === "archived") window.alert("Archive only — relevance correctly suppressed actions.");
    await load();
  } catch (error) { window.alert(`Followthrough failed: ${error.message}`); }
  finally { $("#submit").disabled = false; $("#submit").textContent = "Run through Hermes →"; }
}

async function setMode(mode, resumeParked = false) {
  if (mode === "killed" && !window.confirm("Stop listening and every autonomous capability now?")) return;
  await jsonApi("/api/controls/global", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({mode, reason_code:`dashboard_${mode}`, actor:"owner:dashboard", resume_parked:resumeParked})});
  await load();
}

function setBrowserMic(on) {
  browserListening = on;
  $("#listen").textContent = on ? "Stop browser mic" : "Browser mic";
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
  return `<div class="activity-item${fresh}"><div class="source-icon ${escapeHtml(item.source)}">${item.source === "phone" || item.source === "omi" ? "◉" : "↗"}</div><div class="activity-copy"><div><span>${escapeHtml(label(item.classification))}</span><time>${escapeHtml(timeAgo(item.received_at))}</time></div><p>${escapeHtml(item.text)}</p><small class="${disposition}">${item.relevant ? "Promoted to Hermes" : "Archived · no action"}</small></div></div>`;
}

function jobRow(job) {
  const state = escapeHtml(job.state || "queued");
  const receipt = job.task_id ? escapeHtml(job.task_id) : "pending";
  return `<div class="job-row"><div class="job-name"><span class="job-icon">${escapeHtml(String(job.category || "?").slice(0,1).toUpperCase())}</span><div><strong>${escapeHtml(job.entity || "Identified signal")}</strong><small>${escapeHtml(label(job.category))}</small></div></div><span class="status ${state}"><i></i>${state}</span><code>${receipt}</code><time>${escapeHtml(timeAgo(job.updated_at))}</time></div>`;
}

function memoryCard(item) {
  return `<div class="memory-item"><span>${escapeHtml(String(item.category || "M").slice(0,1).toUpperCase())}</span><div><strong>${escapeHtml(item.entity)}</strong><small>${escapeHtml(label(item.category))} · ${escapeHtml(timeAgo(item.created_at))}</small></div></div>`;
}

function renderCurrentJob(jobs, activity) {
  const active = jobs.filter((job) => !["completed", "cancelled", "dead_letter", "failed"].includes(job.state));
  const job = active[0] || jobs[0];
  $("#activeCount").textContent = `${active.length} active`;
  const latestSignal = activity[0];
  const signalIsNewer = latestSignal && (!job || new Date(latestSignal.received_at) > new Date(job.updated_at));
  if (!active.length && signalIsNewer && !latestSignal.relevant) {
    $("#currentJob").className = "current-job observed";
    $("#currentJob").innerHTML = `<div class="focus-icon quiet">✓</div><span class="focus-state">Latest decision · ${escapeHtml(timeAgo(latestSignal.received_at))}</span><strong>No action required</strong><p>${escapeHtml(label(latestSignal.classification))} · safely archived</p><div class="progress-track"><i></i></div><small>Hermes was not invoked because the relevance gate found no actionable intent.</small>`;
    return;
  }
  if (!job) return;
  $("#currentJob").className = `current-job ${escapeHtml(job.state)}`;
  $("#currentJob").innerHTML = `<div class="focus-icon">H</div><span class="focus-state">${active.length ? "Hermes is working" : "Latest completed work"}</span><strong>${escapeHtml(job.entity || "Identified signal")}</strong><p>${escapeHtml(label(job.hermes_status || job.state))}</p><div class="progress-track"><i></i></div><small>${job.task_id ? `Receipt ${escapeHtml(job.task_id)}` : "Creating durable receipt…"}</small>`;
}

async function load() {
  try {
    const [metrics, jobs, controls, memories, activity] = await Promise.all([
      jsonApi("/api/metrics"), jsonApi("/api/jobs"), jsonApi("/api/controls"), jsonApi("/api/memory/operational"), jsonApi("/api/activity")
    ]);
    const mode = controls.global.mode;
    const healthy = metrics.orchestrator?.status === "ok";
    $("#systemState").innerHTML = `<span class="pulse"></span> ${healthy ? "Spark + Hermes online" : "orchestrator attention"}`;
    $("#systemState").classList.toggle("healthy", healthy);
    $("#lastSync").textContent = `Updated ${new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"})}`;
    $("#modeLabel").textContent = mode.toUpperCase();
    document.body.dataset.mode = mode;
    $("#mArchive").textContent = metrics.total ?? 0;
    $("#mJobs").textContent = jobs.length;
    $("#mDone").textContent = metrics.completed ?? 0;
    $("#activity").innerHTML = activity.length ? activity.slice(0,9).map(activityCard).join("") : '<div class="empty-state"><span>◌</span>Waiting for the phone…</div>';
    if (activity.length) lastActivityId = activity[0].event_id;
    $("#jobs").innerHTML = jobs.length ? jobs.slice(0,12).map(jobRow).join("") : '<div class="empty-state">Waiting for the first relevant signal…</div>';
    $("#jobSummary").textContent = `Live · ${jobs.filter((job) => job.state === "completed").length} completed · ${jobs.filter((job) => !["completed","cancelled","dead_letter","failed"].includes(job.state)).length} active`;
    renderCurrentJob(jobs, activity);
    $("#memoryCount").textContent = `Live · ${memories.length} items`;
    $("#memories").innerHTML = memories.length ? memories.slice(0,7).map(memoryCard).join("") : '<div class="empty-state">Nothing promoted yet.</div>';
  } catch (error) {
    $("#systemState").textContent = `Server unreachable · ${error.message}`;
  }
}

$("#sample").onclick = () => { $("#input").value = "Research and safely evaluate https://github.com/pypa/sampleproject"; };
$("#submit").onclick = () => sendSignal($("#input").value);
$("#listen").onclick = () => setBrowserMic(!browserListening);
load();
setInterval(load, 1500);
