const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
let browserListening = false;
let recognition = null;
let toastTimer = null;
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

async function sendSignal(text, utteranceId = null) {
  if (!text.trim()) return;
  $("#submit").disabled = true;
  $("#submit").textContent = "Archiving and triaging…";
  try {
    const result = await jsonApi("/api/signals", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({text, source:browserListening ? "voice" : "demo", consent:true, ...(utteranceId ? {utterance_id: utteranceId} : {})})});
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

// Interim ASR hypotheses stream to the server (and every open dashboard)
// word by word; only the finalized utterance goes through signal ingestion.
let micUtteranceId = null;
let micUtteranceSeq = 0;

const newUtteranceId = () => (crypto.randomUUID ? crypto.randomUUID() : `utt-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`);

function streamPartial(utteranceId, seq, text) {
  fetch("/api/v1/transcripts/partial", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({utterance_id: utteranceId, device_id: "dashboard", source: "voice", seq, text, consent: true}),
  }).catch(() => {});
}

function setBrowserMic(on) {
  browserListening = on;
  $("#listen").innerHTML = `<span class="mic-dot"></span>${on ? "Stop browser mic" : "Browser mic"}`;
  $("#listen").setAttribute("aria-pressed", String(on));
  if (on && ("webkitSpeechRecognition" in window || "SpeechRecognition" in window)) {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognition = new Recognition(); recognition.continuous = true; recognition.interimResults = true;
    recognition.onresult = (event) => {
      // Walk every updated result: Chrome can bundle a finalized phrase with
      // the next phrase's first interim in one event, and looking only at the
      // last result would drop the final forever.
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const text = result[0].transcript.trim();
        if (!text) continue;
        if (result.isFinal) {
          const finished = micUtteranceId;
          micUtteranceId = null; micUtteranceSeq = 0;
          sendSignal(text, finished);
        } else {
          if (!micUtteranceId) micUtteranceId = newUtteranceId();
          streamPartial(micUtteranceId, micUtteranceSeq++, text);
        }
      }
    };
    recognition.onend = () => {
      // The session died without finalizing the in-flight interim; the next
      // sentence must not stream under the dead utterance's identity.
      micUtteranceId = null; micUtteranceSeq = 0;
      if (browserListening) try { recognition.start(); } catch (_) {}
    };
    recognition.start();
  } else if (!on && recognition) { recognition.stop(); recognition = null; micUtteranceId = null; micUtteranceSeq = 0; }
}

// ---- Transcript tab ---------------------------------------------------------
// Finalized utterances render newest-first with Pacific-time stamps; in-flight
// hypotheses stream into a live area above them and settle into entries when
// the archived event arrives over SSE.
const TRANSCRIPT_PAGE = 50;
const PT_FORMAT = new Intl.DateTimeFormat("en-US", {timeZone: "America/Los_Angeles", year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true});
const ptStamp = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : `${PT_FORMAT.format(date)} PT`;
};

let transcriptEntries = [];
const transcriptIds = new Set();
let transcriptCursor = null;
let transcriptLoaded = false;
let transcriptExhausted = false;
let transcriptLoading = false;
let freshTranscriptId = null;
const livePartials = new Map();
// Utterances that already finalized: a straggler partial that lost the race
// against the final POST must not resurrect a ghost "hearing now" bubble.
const finishedUtterances = new Map();
// Archived events that arrive while the initial page-1 fetch is in flight are
// buffered and merged afterwards instead of being dropped.
let pendingArchived = [];
const LIVE_PARTIAL_CAP = 12;
const LIVE_PARTIAL_TEXT_CAP = 2000;
let liveRenderTimer = null;

function transcriptRow(entry) {
  const fresh = entry.event_id === freshTranscriptId ? " fresh" : "";
  const spokenAt = entry.occurred_at || entry.received_at || "";
  return `<article class="transcript-row${fresh}"><header><span class="transcript-source">${escapeHtml(label(entry.source))}</span><time datetime="${escapeHtml(spokenAt)}">${escapeHtml(ptStamp(spokenAt))}</time>${entry.relevant ? '<span class="transcript-flag">Promoted to Hermes</span>' : ""}</header><p>${escapeHtml(entry.text)}</p></article>`;
}

function renderTranscript() {
  setHTML("#transcriptEntries", transcriptEntries.length
    ? transcriptEntries.map(transcriptRow).join("")
    : '<div class="empty-state"><span class="empty-orbit"></span><strong>Nothing transcribed yet</strong><small>Speak near the phone or use the browser mic and words will land here live.</small></div>');
  $("#transcriptMore").hidden = transcriptExhausted || !transcriptEntries.length;
}

function renderTranscriptLive() {
  setHTML("#transcriptLive", [...livePartials.values()]
    .sort((a, b) => b.localAt - a.localAt)
    .slice(0, 6)
    .map((partial) => `<div class="transcript-partial"><span class="live-dot"></span><div><small>${escapeHtml(label(partial.source))} · hearing now</small><p>${escapeHtml(String(partial.text).slice(0, LIVE_PARTIAL_TEXT_CAP))}</p></div></div>`)
    .join(""));
}

// Partial events can burst several times a second; coalesce repaints so a
// flood costs one render per frame-ish window instead of one per event.
function scheduleLiveRender() {
  if (liveRenderTimer) return;
  liveRenderTimer = setTimeout(() => { liveRenderTimer = null; renderTranscriptLive(); }, 80);
}

// Insert while preserving the strict newest-first invariant even when SSE
// arrival order and head refreshes interleave.
function insertTranscriptEntry(entry, {fresh = false} = {}) {
  if (!entry || !entry.event_id || transcriptIds.has(entry.event_id)) return;
  transcriptIds.add(entry.event_id);
  transcriptEntries.push(entry);
  transcriptEntries.sort((a, b) => String(b.received_at ?? "").localeCompare(String(a.received_at ?? "")));
  if (fresh) freshTranscriptId = entry.event_id;
  renderTranscript();
}

// SSE delivery is lossy (reconnects, bounded queues): re-pull page 1 and merge
// anything missed. Dedupe by event_id makes this idempotent and cheap.
async function refreshTranscriptHead() {
  if (!transcriptLoaded) return;
  try {
    const batch = await jsonApi(`/api/transcript?limit=${TRANSCRIPT_PAGE}`);
    for (const entry of batch) insertTranscriptEntry(entry);
  } catch (_) { /* the next reconnect or tab open retries */ }
}

async function loadTranscript(reset = false) {
  if (transcriptLoading) return;
  transcriptLoading = true;
  try {
    const params = new URLSearchParams({limit: String(TRANSCRIPT_PAGE)});
    if (!reset && transcriptCursor) {
      params.set("before", transcriptCursor.receivedAt);
      params.set("before_id", transcriptCursor.id);
    }
    const batch = await jsonApi(`/api/transcript?${params}`);
    if (reset) { transcriptEntries = []; transcriptIds.clear(); transcriptCursor = null; transcriptExhausted = false; }
    for (const entry of batch) {
      if (transcriptIds.has(entry.event_id)) continue;
      transcriptIds.add(entry.event_id);
      transcriptEntries.push(entry);
    }
    if (batch.length) transcriptCursor = {
      receivedAt: batch[batch.length - 1].received_at,
      id: batch[batch.length - 1].archive_id,
    };
    if (batch.length < TRANSCRIPT_PAGE) transcriptExhausted = true;
    transcriptLoaded = true;
    const buffered = pendingArchived;
    pendingArchived = [];
    for (const entry of buffered) insertTranscriptEntry(entry, {fresh: true});
    renderTranscript();
  } catch (error) {
    showToast(`Transcript load failed: ${error.message}`, "error");
  } finally {
    transcriptLoading = false;
  }
}

function setView(view) {
  const transcript = view === "transcript";
  document.body.dataset.view = view;
  $("#top").hidden = transcript;
  $("#transcriptView").hidden = !transcript;
  $("#tabOverview").classList.toggle("active", !transcript);
  $("#tabOverview").setAttribute("aria-pressed", String(!transcript));
  $("#tabTranscript").classList.toggle("active", transcript);
  $("#tabTranscript").setAttribute("aria-pressed", String(transcript));
  if (transcript) transcriptLoaded ? refreshTranscriptHead() : loadTranscript(true);
  const hash = transcript ? "#transcript" : "";
  if (location.hash !== hash) history.replaceState(null, "", hash || location.pathname + location.search);
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
  const terminalStates = new Set(["completed", "cancelled", "dead_letter", "failed", "needs_attention"]);
  const active = jobs.filter((job) => !terminalStates.has(job.state));
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


// ---- Interactive desktop control -------------------------------------------
// The panel paints the frame stream; these handlers send the operator's mouse
// and keyboard back to the same typed API the agent uses. Verification is off
// for human input: a person can see the result, and the round trip stays fast.
let desktopControl = false;
let desktopBusy = false;

const DESKTOP_KEYS = {
  Enter: "Return", Backspace: "BackSpace", Tab: "Tab", Escape: "Escape",
  ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
  Delete: "Delete", Home: "Home", End: "End", PageUp: "Prior", PageDown: "Next",
  " ": "space",
};

function desktopPoint(event) {
  const image = $("#desktopFrame");
  const box = image.getBoundingClientRect();
  // The frame is letterboxed by object-fit: contain, so map through the
  // rendered image rather than the element box or every click lands skewed.
  const scale = Math.min(box.width / image.naturalWidth, box.height / image.naturalHeight);
  const shownWidth = image.naturalWidth * scale;
  const shownHeight = image.naturalHeight * scale;
  const offsetX = (box.width - shownWidth) / 2;
  const offsetY = (box.height - shownHeight) / 2;
  const x = Math.round((event.clientX - box.left - offsetX) / scale);
  const y = Math.round((event.clientY - box.top - offsetY) / scale);
  if (x < 0 || y < 0 || x > image.naturalWidth || y > image.naturalHeight) return null;
  return {x, y};
}

async function desktopSend(action, body) {
  if (desktopBusy) return;
  desktopBusy = true;
  try {
    await jsonApi(`/api/desktop/${action}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...body, verify: false}),
    });
    $("#desktopFrame").src = `/api/desktop/screenshot?t=${Date.now()}`;
  } catch (error) {
    showToast(`Desktop ${action} failed: ${error.message}`, "error");
  } finally {
    desktopBusy = false;
  }
}

function setDesktopControl(on) {
  desktopControl = on;
  const button = $("#desktopControl");
  const screen = document.querySelector(".desktop-screen");
  if (button) {
    button.setAttribute("aria-pressed", String(on));
    button.textContent = on ? "Stop controlling" : "Take control";
  }
  if (screen) screen.classList.toggle("controllable", on);
  if (on) $("#desktopFrame").focus();
}

function wireDesktopControl() {
  const image = $("#desktopFrame");
  if (!image) return;
  image.tabIndex = 0;

  image.addEventListener("click", (event) => {
    if (!desktopControl) return;
    const point = desktopPoint(event);
    if (point) desktopSend("click", {...point, button: "left", double: event.detail > 1});
  });
  image.addEventListener("contextmenu", (event) => {
    if (!desktopControl) return;
    event.preventDefault();
    const point = desktopPoint(event);
    if (point) desktopSend("click", {...point, button: "right"});
  });
  image.addEventListener("wheel", (event) => {
    if (!desktopControl) return;
    event.preventDefault();
    desktopSend("scroll", {direction: event.deltaY > 0 ? "down" : "up", amount: 3});
  }, {passive: false});
  image.addEventListener("keydown", (event) => {
    if (!desktopControl) return;
    event.preventDefault();
    const mapped = DESKTOP_KEYS[event.key];
    if (mapped) return void desktopSend("key", {key: mapped});
    if (event.ctrlKey || event.metaKey || event.altKey) {
      const chord = [event.ctrlKey && "ctrl", event.altKey && "alt", event.key.length === 1 && event.key]
        .filter(Boolean).join("+");
      if (chord.includes("+")) desktopSend("key", {key: chord});
      return;
    }
    if (event.key.length === 1) desktopSend("type", {text: event.key, delay_ms: 0});
  });

  const button = $("#desktopControl");
  if (button) button.onclick = () => setDesktopControl(!desktopControl);
}

function renderDesktop(doctor, actions) {
  const state = $("#desktopState");
  const frame = $("#desktopFrame");
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
    // Every plane paints from the screenshot stream. The VNC canvas needed a
    // connector that was never wired, so the panel sat on "Connecting…".
    frame.hidden = false;
    frame.src = `/api/desktop/screenshot?t=${Date.now()}`;
  } else {
    frame.hidden = true; empty.hidden = false;
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
    if (/^#{1,6}\s+/.test(line)) {
      // The agent sometimes answers with markdown headings.
      flush();
      html.push(`<p class="answer-head">${line.replace(/^#{1,6}\s+/, "")}</p>`);
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      bullets.push(`<li>${line.replace(/^\d+\.\s+/, "")}</li>`);
      continue;
    }
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
    const [metrics, jobs, controls, memories, activity, desktopDoctor, desktopActions, agentSessions] = await Promise.all([
      jsonApi("/api/metrics"), jsonApi("/api/jobs"), jsonApi("/api/controls"), jsonApi("/api/memory/operational"), jsonApi("/api/activity"), jsonApi("/api/desktop/doctor"), jsonApi("/api/desktop/actions"), jsonApi("/api/computer-use")
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
    setHTML("#jobs", jobs.length ? jobs.slice(0,12).map(jobRow).join("") : '<div class="empty-state compact"><strong>No delegated work yet</strong><small>Qualified signals will become traceable work here.</small></div>');
    $("#jobSummary").textContent = `Live · ${jobs.filter((job) => job.state === "completed").length} completed · ${jobs.filter((job) => !["completed","cancelled","dead_letter","failed","needs_attention"].includes(job.state)).length} active`;
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
$("#tabOverview").onclick = () => setView("overview");
$("#tabTranscript").onclick = () => setView("transcript");
$("#openTranscript").onclick = () => setView("transcript");
$("#transcriptMore").onclick = () => loadTranscript();
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
wireDesktopControl();
load();
setInterval(load, 1500);
const liveEvents = new EventSource("/api/events");
liveEvents.addEventListener("desktop_action", () => load());
liveEvents.addEventListener("computer_use_progress", () => load());
liveEvents.addEventListener("computer_use_completed", () => load());
// Each (re)connect opens with a "ready" frame: refresh page 1 so anything
// published while disconnected (or shed by the bounded queue) is recovered.
liveEvents.addEventListener("ready", () => refreshTranscriptHead());
liveEvents.addEventListener("transcript_partial", (event) => {
  const {payload} = JSON.parse(event.data);
  if (finishedUtterances.has(payload.utterance_id)) return;
  const current = livePartials.get(payload.utterance_id);
  // Interim hypotheses can arrive out of order; an older frame never
  // overwrites a newer one within the same utterance.
  if (current && current.seq > payload.seq) return;
  if (!current && livePartials.size >= LIVE_PARTIAL_CAP) {
    // Bound the map: shed the stalest bubble rather than growing forever.
    let stalest = null;
    for (const [key, value] of livePartials) if (!stalest || value.localAt < livePartials.get(stalest).localAt) stalest = key;
    livePartials.delete(stalest);
  }
  livePartials.set(payload.utterance_id, {...payload, localAt: Date.now()});
  scheduleLiveRender();
});
liveEvents.addEventListener("transcript_archived", (event) => {
  const {payload} = JSON.parse(event.data);
  if (payload.utterance_id) {
    finishedUtterances.set(payload.utterance_id, Date.now());
    if (livePartials.delete(payload.utterance_id)) scheduleLiveRender();
  }
  if (payload.aggregated) return;
  if (!transcriptLoaded) { pendingArchived.push(payload); return; }
  insertTranscriptEntry(payload, {fresh: true});
});
// A hypothesis whose utterance never finalizes (mic cut out, device offline)
// decays instead of pulsing forever; finished-utterance tombstones expire too.
setInterval(() => {
  let changed = false;
  for (const [key, partial] of livePartials) {
    if (Date.now() - partial.localAt > 15000) { livePartials.delete(key); changed = true; }
  }
  for (const [key, at] of finishedUtterances) {
    if (Date.now() - at > 60000) finishedUtterances.delete(key);
  }
  if (changed) scheduleLiveRender();
}, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshTranscriptHead(); });
// Reaching the bottom of the transcript pulls the next (older) page without a click.
new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting) && transcriptLoaded && !transcriptExhausted) loadTranscript();
}).observe($("#transcriptMore"));
// The brand link and manual hash edits navigate views too, so the URL and the
// visible view never disagree.
document.querySelector(".brand").addEventListener("click", (event) => {
  event.preventDefault();
  setView("overview");
  window.scrollTo({top: 0});
});
window.addEventListener("hashchange", () => setView(location.hash === "#transcript" ? "transcript" : "overview"));
setView(location.hash === "#transcript" ? "transcript" : "overview");
