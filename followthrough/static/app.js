const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
let browserListening = false;
let recognition = null;

function dashboardToken(force = false) {
  let value = force ? "" : localStorage.getItem("followthrough_dashboard_token");
  if (!value) {
    value = window.prompt("Enter your Followthrough dashboard token") || "";
    if (value) localStorage.setItem("followthrough_dashboard_token", value.trim());
  }
  return value.trim();
}

async function api(path, options = {}, retry = true) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${dashboardToken()}`);
  const response = await fetch(path, {...options, headers});
  if (response.status === 401 && retry) {
    localStorage.removeItem("followthrough_dashboard_token");
    dashboardToken(true);
    return api(path, options, false);
  }
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response;
}

async function jsonApi(path, options = {}) { return (await api(path, options)).json(); }

async function sendSignal(text) {
  if (!text.trim()) return;
  $("#submit").disabled = true;
  $("#submit").textContent = "Archiving and triaging…";
  try {
    const result = await jsonApi("/api/signals", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({text, source:browserListening ? "voice" : "demo", consent:true})});
    if (result.status === "archived") window.alert("Encrypted archive only: relevance gate correctly suppressed Hermes and actions.");
    await load();
  } catch (error) {
    window.alert(`Followthrough failed: ${error.message}`);
  } finally {
    $("#submit").disabled = false;
    $("#submit").textContent = "Send through the full pipeline →";
  }
}

async function setMode(mode, resumeParked = false) {
  if (mode === "killed" && !window.confirm("Stop listening and every autonomous capability now?")) return;
  await jsonApi("/api/controls/global", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({mode, reason_code:`dashboard_${mode}`, actor:"owner:dashboard", resume_parked:resumeParked})});
  await load();
}

function setBrowserMic(on) {
  browserListening = on;
  $("#listen").textContent = on ? "Stop browser mic" : "Browser mic demo";
  if (on && ("webkitSpeechRecognition" in window || "SpeechRecognition" in window)) {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognition = new Recognition(); recognition.continuous = true; recognition.interimResults = false;
    recognition.onresult = (event) => sendSignal(event.results[event.results.length - 1][0].transcript);
    recognition.onend = () => { if (browserListening) try { recognition.start(); } catch (_) {} };
    recognition.start();
  } else if (!on && recognition) { recognition.stop(); recognition = null; }
}

function jobCard(job) {
  const state = escapeHtml(job.state || "unknown");
  const entity = escapeHtml(job.entity || "identified signal");
  const task = job.task_id ? ` · ${escapeHtml(job.task_id)}` : "";
  return `<div class="run"><div class="run-top"><strong>${escapeHtml(job.category)}</strong><span class="status ${state}">${state}</span></div><div class="entity">${entity}</div><div class="step">${escapeHtml(job.hermes_status || "queued")}${task} · ${escapeHtml(new Date(job.updated_at).toLocaleTimeString())}</div></div>`;
}

function memoryCard(item) {
  return `<div class="run"><div class="run-top"><strong>${escapeHtml(item.category)}</strong><span class="status">promoted</span></div><div class="entity">${escapeHtml(item.entity)}</div><div class="step">fingerprint ${escapeHtml(String(item.content_fingerprint).slice(0,12))} · ${escapeHtml(new Date(item.created_at).toLocaleTimeString())}</div></div>`;
}

async function load() {
  try {
    const [metrics, jobs, controls, memories] = await Promise.all([
      jsonApi("/api/metrics"), jsonApi("/api/jobs"), jsonApi("/api/controls"), jsonApi("/api/memory/operational")
    ]);
    const mode = controls.global.mode;
    const healthy = metrics.orchestrator?.status === "ok";
    $("#systemState").innerHTML = `<span class="pulse"></span> ${healthy ? "Spark + Hermes online" : "orchestrator attention"}`;
    $("#systemState").classList.toggle("healthy", healthy);
    $("#modeLabel").textContent = mode.toUpperCase();
    $("#controlTitle").textContent = mode === "running" ? "Autonomy is running" : mode === "paused" ? "Actions are paused; archive remains on" : "Global kill latch is active";
    $("#controlDetail").textContent = `Generation ${controls.global.generation}. Audit chain ${controls.audit_chain_valid ? "valid" : "FAILED"}. ${Object.values(controls.capabilities).filter((value) => value.enabled).length} capabilities enabled.`;
    document.body.dataset.mode = mode;
    $("#mArchive").textContent = metrics.total ?? 0;
    $("#mAudio").textContent = metrics.audio_chunks ?? 0;
    $("#mJobs").textContent = jobs.length;
    $("#mDone").textContent = metrics.completed ?? 0;
    $("#mLatency").textContent = metrics.avg_latency_ms ? `${metrics.avg_latency_ms}ms` : "—";
    $("#jobs").innerHTML = jobs.length ? jobs.slice(0,10).map(jobCard).join("") : '<div class="empty">Waiting for a job…</div>';
    $("#memoryCount").textContent = `${memories.length} items`;
    $("#memories").innerHTML = memories.length ? memories.slice(0,8).map(memoryCard).join("") : '<div class="empty">No promoted signals.</div>';
    const integrations = {...(metrics.integrations || {}), encrypted_archive:metrics.total >= 0, durable_kanban:Boolean(metrics.orchestrator), emergency_controls:controls.audit_chain_valid};
    $("#integrations").innerHTML = Object.entries(integrations).map(([name, enabled]) => `<span class="integration ${enabled ? "on" : ""}">${enabled ? "✓" : "○"} ${escapeHtml(name.replaceAll("_"," "))}</span>`).join("");
  } catch (error) {
    $("#systemState").textContent = `Owner authentication required · ${error.message}`;
  }
}

$("#pauseActions").onclick = () => setMode("paused");
$("#resumeActions").onclick = () => setMode("running", true);
$("#killAll").onclick = () => setMode("killed");
$("#sample").onclick = () => { $("#input").value = "Research and safely evaluate https://github.com/pypa/sampleproject"; };
$("#submit").onclick = () => sendSignal($("#input").value);
$("#listen").onclick = () => setBrowserMic(!browserListening);
load();
setInterval(load, 2000);
