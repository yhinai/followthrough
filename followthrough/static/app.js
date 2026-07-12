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

function renderDesktop(doctor, actions) {
  const state = $("#desktopState");
  const frame = $("#desktopFrame");
  const live = $("#desktopLive");
  const empty = $("#desktopEmpty");
  state.className = `desktop-state ${doctor.ready ? "online" : "offline"}`;
  state.innerHTML = `<i></i>${doctor.ready ? "Live" : "Not configured"}`;
  $("#desktopProvider").textContent = doctor.provider || doctor.prefer || "Unavailable";
  $("#desktopComputer").textContent = doctor.computer_id ? doctor.computer_id.slice(0, 12) : (doctor.provider === "spark-local" ? "This computer" : "—");
  $("#desktopRoute").textContent = doctor.ready ? `${label(doctor.provider)} · automatic` : "Automatic routing";
  const shot = doctor.screenshot || {};
  $("#desktopResolution").textContent = shot.width ? `${shot.width} × ${shot.height}` : "—";
  if (doctor.ready) {
    empty.hidden = true;
    if (doctor.provider === "spark-local") {
      frame.hidden = true; live.hidden = false;
      if (!live.src) live.src = "/static/desktop-viewer.html";
    } else {
      live.hidden = true; frame.hidden = false;
      frame.src = `/api/desktop/screenshot?t=${Date.now()}`;
    }
  } else {
    frame.hidden = true; live.hidden = true; empty.hidden = false;
  }
  const action = actions[0];
  $("#desktopAction").textContent = action ? label(action.action) : "No desktop action yet";
  $("#desktopVerification").textContent = action
    ? (action.noop === 1 ? "No visual change detected — the agent must re-plan." : action.visual_changed === 1 ? "Visual change verified from frame fingerprints." : "Action recorded without visual verification.")
    : "Before/after visual fingerprints will appear here.";
  setHTML("#desktopTimeline", actions.slice(0, 4).map((item, index) => `<li class="${index === 0 ? "current" : ""}"><i></i><span>${escapeHtml(label(item.action))}</span><small>${item.noop === 1 ? "Re-plan" : item.visual_changed === 1 ? "Verified" : "Recorded"}</small></li>`).join(""));
}

const AGENT_LIVE = new Set(["starting", "queued", "pending", "running"]);
const AGENT_MAX_STEPS = 12;

function formatAgentAnswer(value) {
  const lines = escapeHtml(String(value).slice(0, 900))
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .split(/\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const html = [];
  let bullets = [];
  const flush = () => {
    if (bullets.length) { html.push(`<ul>${bullets.join("")}</ul>`); bullets = []; }
  };
  for (const line of lines) {
    if (/^[-*\u2022\u00b7]\s+/.test(line)) {
      bullets.push(`<li>${line.replace(/^[-*\u2022\u00b7]\s+/, "")}</li>`);
    } else {
      flush();
      html.push(`<p>${line}</p>`);
    }
  }
  flush();
  return html.join("");
}

function agentActionText(session) {
  const raw = String(session.current_action || "");
  // The raw H event is a JSON envelope; show the agent's thought when it has one.
  const thought = raw.match(/"thought"\s*:\s*"([^"]{4,160})/);
  if (thought) return thought[1];
  const kind = raw.match(/^([A-Za-z]+Event)/);
  if (kind) return kind[1].replace(/([a-z])([A-Z])/g, "$1 $2");
  return raw.slice(0, 140) || "Working…";
}

function renderAgent(sessions) {
  if (!$("#agentState")) return;
  const list = Array.isArray(sessions) ? sessions : [];
  const live = list.find((session) => AGENT_LIVE.has(session.state));
  const session = live || list[0];
  const chip = $("#agentState");
  const card = $("#agentCurrent");

  if (!session) {
    chip.className = "agent-state idle";
    setHTML("#agentState", "<i></i>Idle");
    card.className = "agent-current empty";
    return;
  }

  const running = AGENT_LIVE.has(session.state);
  chip.className = `agent-state ${running ? "working" : escapeHtml(session.state)}`;
  setHTML("#agentState", `<i></i>${running ? "Agent working" : escapeHtml(label(session.state))}`);
  card.className = `agent-current ${running ? "working" : escapeHtml(session.state)}`;

  $("#agentTask").textContent = session.task || "Web task";
  $("#agentAction").textContent = running
    ? agentActionText(session)
    : (session.error ? String(session.error).slice(0, 160) : "Finished");
  const steps = Number(session.step_count || 0);
  $("#agentSteps").textContent = `${steps} step${steps === 1 ? "" : "s"}`;
  $("#agentAgent").textContent = session.agent || "h/web-surfer-flash";
  $("#agentBar").style.width = `${Math.min(100, Math.round((steps / AGENT_MAX_STEPS) * 100))}%`;

  const replay = $("#agentReplay");
  if (session.agent_view_url) {
    replay.href = session.agent_view_url;
    replay.hidden = false;
  } else {
    replay.hidden = true;
  }

  const screen = $("#agentScreen");
  const frame = $("#agentFrame");
  if (session.latest_frame_url) {
    // Cache-bust on the step counter so each new step repaints the frame.
    const next = `/api/computer-use/${encodeURIComponent(session.id)}/frame?step=${steps}`;
    if (frame.getAttribute("src") !== next) frame.setAttribute("src", next);
    screen.hidden = false;
  } else {
    screen.hidden = true;
  }

  const answer = $("#agentAnswer");
  if (session.state === "completed" && session.latest_answer) {
    answer.hidden = false;
    setHTML("#agentAnswer", `<small>Returned to the phone</small>${formatAgentAnswer(session.latest_answer)}`);
  } else {
    answer.hidden = true;
  }

  const history = list.filter((item) => item.id !== session.id).slice(0, 3);
  setHTML("#agentSessions", history.map((item) => `
    <li class="agent-session ${escapeHtml(item.state)}">
      <span class="agent-dot"></span>
      <div><strong>${escapeHtml(String(item.task || "web task").slice(0, 70))}</strong>
      <small>${escapeHtml(label(item.state))} · ${Number(item.step_count || 0)} steps · ${escapeHtml(timeAgo(item.updated_at))}</small></div>
      ${item.agent_view_url ? `<a href="${escapeHtml(item.agent_view_url)}" target="_blank" rel="noopener">replay ↗</a>` : ""}
    </li>`).join(""));
}

function renderJourney(activity, sessions, jobs) {
  const session = sessions[0];
  const signal = session ? activity.find((item) => item.event_id === session.source_event_id) : activity.find((item) => item.relevant);
  const job = session ? jobs.find((item) => item.event_id === session.source_event_id) : null;
  const browsing = session && AGENT_LIVE.has(session.state);
  const verified = session?.state === "completed" && Boolean(session.latest_answer);
  const returned = verified && job?.last_polled_at && new Date(job.last_polled_at) >= new Date(session.finished_at || session.updated_at);
  [Boolean(signal), Boolean(signal?.relevant), Boolean(session), Boolean(browsing || verified), Boolean(verified), Boolean(returned)].forEach((done, index) => document.querySelectorAll("#journeyStages li")[index]?.classList.toggle("done", done));
  $("#journeyHeard").textContent = signal ? `Heard: “${String(signal.text).slice(0, 88)}”` : "Listening for the demo phrase…";
  $("#journeyDecision").textContent = signal?.relevant ? "Relevant signal detected · Web task created" : "Mention it, put your phone away, and Followthrough handles it.";
  if (session?.created_at) { const end = session.finished_at || session.updated_at; const seconds = Math.max(0, Math.round((new Date(end) - new Date(session.created_at)) / 1000)); $("#journeyElapsed").textContent = verified ? `Completed in ${seconds}s` : `Working for ${seconds}s`; }
  $("#journeyPhone").textContent = returned ? "Returned to Samsung Flip · playback ready" : verified ? "Result ready · awaiting phone poll" : "Runs quietly in the background";
}

async function load() {
  try {
    const [metrics, jobs, controls, memories, activity, desktopDoctor, desktopActions, agentSessions, journey] = await Promise.all([
      jsonApi("/api/metrics"), jsonApi("/api/jobs"), jsonApi("/api/controls"), jsonApi("/api/memory/operational"), jsonApi("/api/activity"), jsonApi("/api/desktop/doctor"), jsonApi("/api/desktop/actions"), jsonApi("/api/computer-use"), jsonApi("/api/journey")
    ]);
    renderAgent(agentSessions);
    renderJourney(activity, agentSessions, jobs);
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
    renderDesktop(desktopDoctor, desktopActions);
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
$("#refreshDesktop").onclick = () => load();
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
const liveEvents = new EventSource("/api/events");
liveEvents.addEventListener("desktop_action", () => load());
liveEvents.addEventListener("computer_use_progress", () => load());
liveEvents.addEventListener("computer_use_completed", () => load());
