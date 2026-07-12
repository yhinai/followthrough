from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .adb_bridge import Transcript, TranscriptAggregator
from .archive import ArchiveStorage
from .archive_store import ArchiveStore
from .bus import bus
from .classifier import Classification
from .config import Settings, settings
from .controls import Capability, ControlPlane
from .desktop import DesktopError, DesktopRouter
from .hcompany import TERMINAL_STATES as H_TERMINAL_STATES, HCompanyExecutor
from .integrations import operational_entity, start_url
from .models import (
    CapabilityControlIn,
    CapabilityLimitIn,
    ComputerUseIn,
    DesktopClickIn,
    DesktopDragIn,
    DesktopKeyIn,
    DesktopLifecycleIn,
    DesktopScrollIn,
    DesktopTypeIn,
    GlobalControlIn,
    InterestWeightIn,
    LiveKitSessionIn,
    RelevanceCorrectionIn,
    SafeModeIn,
    SignalIn,
    TaskControlIn,
    TranscriptEventIn,
    TranscriptPartialIn,
    WorkspaceItemIn,
)
from .livekit_tokens import issue_memo_session_token
from .relevance import (
    Category,
    CorrectionRecord,
    InterestWeight,
    OwnerStatus,
    SpeakerContext,
    evaluate_relevance,
)
from .store import Store


def _utc(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _safe_child(parent: Path, filename: str) -> Path:
    candidate = (parent / filename).resolve()
    if candidate.parent != parent.resolve():
        raise HTTPException(status_code=404, detail="file not found")
    return candidate


def _omi_audio_delivery_key(
    *,
    device_identity: str,
    uid: str,
    sample_rate: int,
    payload_digest: str,
    timestamp: str | None = None,
    sequence: str | None = None,
    delivery_nonce: str | None = None,
) -> str:
    """Derive the stable delivery key omitted by Omi's official audio webhook.

    The caller supplies a stable device identity. The payload is bounded by
    ``max_audio_chunk_bytes`` before its
    digest reaches this helper. Future Omi clients can disambiguate identical
    chunks by supplying a timestamp or sequence without changing the contract.
    """

    material = "\0".join(
        (
            "omi-audio-v1",
            device_identity,
            uid,
            str(sample_rate),
            payload_digest,
            (timestamp or "").strip(),
            (sequence or "").strip(),
            (delivery_nonce or "").strip(),
        )
    )
    return f"derived-{hashlib.sha256(material.encode()).hexdigest()}"


def _omi_text(payload: dict[str, Any]) -> str:
    direct = payload.get("transcript") or payload.get("text")
    if isinstance(direct, str):
        return direct.strip()
    memory = payload.get("memory")
    if isinstance(memory, dict):
        for key in ("transcript", "text", "content"):
            if isinstance(memory.get(key), str):
                return memory[key].strip()
    segments = payload.get("segments") or payload.get("transcript_segments")
    if isinstance(segments, list):
        values = []
        for segment in segments:
            if isinstance(segment, str):
                values.append(segment)
            elif isinstance(segment, dict):
                text = segment.get("text") or segment.get("transcript")
                if isinstance(text, str):
                    values.append(text)
        return " ".join(values).strip()
    return ""


def _omi_event(payload: dict[str, Any], device_header: str | None = None) -> TranscriptEventIn:
    text = _omi_text(payload)
    if not text:
        raise HTTPException(status_code=422, detail="no transcript found in Omi payload")
    device_id = str(device_header or payload.get("device_id") or payload.get("deviceId") or "omi")
    occurred = payload.get("occurred_at") or payload.get("created_at") or payload.get("timestamp")
    event_id = payload.get("event_id") or payload.get("id")
    if not event_id and isinstance(payload.get("memory"), dict):
        event_id = payload["memory"].get("id")
    if not event_id:
        stable = json.dumps(
            {"device": device_id, "occurred": occurred, "text": text}, sort_keys=True
        ).encode()
        event_id = "omi:" + hashlib.sha256(stable).hexdigest()
    metadata = {
        key: payload[key]
        for key in ("session_id", "language", "speaker", "speaker_id", "is_user", "app_id")
        if key in payload
    }
    try:
        return TranscriptEventIn(
            event_id=str(event_id),
            device_id=device_id,
            text=text,
            source="omi",
            occurred_at=occurred,
            consent=True,
            metadata=metadata,
        )
    except ValidationError as exc:
        # A malformed client-supplied timestamp is a bad request, not a server
        # fault; surface it as 422 instead of an unhandled 500.
        raise HTTPException(status_code=422, detail="invalid Omi transcript field") from exc


def _omi_segment_events(
    payload: Any, uid: str, idempotency_key: str | None, hook: str
) -> list[tuple[TranscriptEventIn, bool | None]]:
    if isinstance(payload, list):
        segments = payload
        envelope: dict[str, Any] = {}
    elif isinstance(payload, dict):
        segments = payload.get("segments") or payload.get("transcript_segments") or []
        envelope = payload
    else:
        raise HTTPException(
            status_code=422, detail="Omi transcript payload must be an object or array"
        )
    if not isinstance(segments, list) or not segments:
        if isinstance(payload, dict) and _omi_text(payload):
            event = _omi_event(payload, uid)
            raw_owner = payload.get("is_user")
            return [(event, raw_owner if isinstance(raw_owner, bool) else None)]
        raise HTTPException(status_code=422, detail="Omi payload contains no transcript segments")
    events: list[tuple[TranscriptEventIn, bool | None]] = []
    for index, segment in enumerate(segments):
        if isinstance(segment, str):
            text = segment.strip()
            metadata: dict[str, Any] = {"hook": hook, "index": index}
            segment_id = None
            is_user = None
        elif isinstance(segment, dict):
            text = str(segment.get("text") or segment.get("transcript") or "").strip()
            segment_id = segment.get("id")
            raw_owner = segment.get("is_user")
            is_user = raw_owner if isinstance(raw_owner, bool) else None
            metadata = {
                "hook": hook,
                "index": index,
                "session_id": envelope.get("session_id"),
                "conversation_id": envelope.get("conversation_id") or envelope.get("id"),
                "speaker": segment.get("speaker"),
                "speaker_id": segment.get("speaker_id", segment.get("speakerId")),
                "is_user": is_user,
                "person_id": segment.get("person_id", segment.get("personId")),
                "start": segment.get("start"),
                "end": segment.get("end"),
                "stt_provider": segment.get("stt_provider"),
            }
        else:
            continue
        if not text:
            continue
        if segment_id:
            identity = str(segment_id)
        else:
            stable_segment = json.dumps(
                {
                    "uid": uid,
                    "text": text,
                    "speaker_id": metadata.get("speaker_id"),
                    "start": metadata.get("start"),
                    "end": metadata.get("end"),
                },
                sort_keys=True,
            ).encode()
            identity = hashlib.sha256(stable_segment).hexdigest()
        event_id = f"omi:segment:{uid}:{identity}"
        metadata.update(
            {
                "observed_hooks": [hook],
                "delivery_idempotency_key": idempotency_key,
                "conversation_started_at": envelope.get("started_at") or envelope.get("start_date"),
                "conversation_finished_at": envelope.get("finished_at") or envelope.get("end_date"),
            }
        )
        events.append(
            (
                TranscriptEventIn(
                    event_id=event_id,
                    device_id=uid,
                    text=text,
                    source="omi",
                    consent=True,
                    metadata=metadata,
                ),
                is_user,
            )
        )
    if not events:
        raise HTTPException(
            status_code=422, detail="Omi payload contains no non-empty transcript segments"
        )
    return events


def create_app(config: Settings = settings) -> FastAPI:
    store = Store(config.db_path)
    archive_store = ArchiveStore(config.archive_db_path)
    archive = ArchiveStorage(config.audio_dir)
    controls = ControlPlane(store)
    h_executor = HCompanyExecutor(store, config, bus.publish)
    desktop = DesktopRouter(store, config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # A restart kills the tasks watching live H sessions, but the sessions
        # themselves keep running. Re-attach so their answers still reach the
        # phone instead of leaving the job pending forever.
        for orphan in store.unfinished_computer_sessions():
            task = asyncio.create_task(h_executor.resume(orphan["id"]))
            application.state.background_tasks.add(task)
            task.add_done_callback(application.state.background_tasks.discard)
        yield

    application = FastAPI(
        title="Followthrough",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.settings = config
    application.state.store = store
    application.state.archive_store = archive_store
    application.state.archive = archive
    application.state.controls = controls
    application.state.h_executor = h_executor
    application.state.desktop = desktop
    application.state.background_tasks = set()
    application.state.transcript_aggregators = {}
    static = Path(__file__).parent / "static"
    if static.is_dir():
        application.mount("/static", StaticFiles(directory=static), name="static")
    novnc = Path("/usr/share/novnc")
    if novnc.is_dir():
        application.mount("/novnc", StaticFiles(directory=novnc, html=True), name="novnc")

    @application.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), payment=()"
        if request.url.path.startswith("/novnc/") or request.url.path.startswith(
            "/static/desktop-viewer"
        ):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' https://static.cloudflareinsights.com; "
                "style-src 'self'; font-src 'self'; "
                "connect-src 'self' https://cloudflareinsights.com wss://*.livekit.cloud; "
                "img-src 'self' data:; media-src 'self' blob:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authorize_capture(idempotency_key: str, actor: str) -> None:
        decision = controls.authorize(
            Capability.LISTENING,
            idempotency_key=idempotency_key,
            actor=actor,
        )
        if not decision.allowed:
            raise HTTPException(status_code=503, detail=decision.reason_code)

    async def enqueue_durable_job(
        run_id: str,
        archive_id: str,
        event_id: str,
        category: str,
        named_entity: str,
        *,
        deferred: bool = False,
    ) -> dict[str, object]:
        job_id = str(uuid.uuid4())
        capsule_name = hashlib.sha256(run_id.encode()).hexdigest()[:24] + ".json"
        capsule_path = config.jobs_dir / capsule_name
        idempotency_key = f"followthrough:{archive_id}:research:v3"
        intent = {
            "repository": "Research and evaluate the named repository, inspect provenance and security, then run only safe sandboxed tests that add decision value.",
            "tool": "Research the named tool from primary sources, compare fit to the user's agent workflow, and propose a verified next action.",
            "performance": "Research and test the named optimization using reproducible measurements without changing production defaults.",
            "event": "Research the named event, extract verified logistics and useful preparation actions.",
            "todo": "Turn the captured commitment into a concrete private task with a verifiable completion condition.",
            "web_task": "Track the separately started H Company computer-use session. The typed H runner owns browsing; Hermes owns the durable task, receipt, and delivery lifecycle.",
        }.get(
            category,
            "Research and evaluate the named item, then return cited findings and a safe next action.",
        )
        acceptance = [
            "Use official or primary sources and preserve direct URLs",
            "Separate verified facts from inference",
            "For repositories, record commit provenance, license, policy findings, and sandbox receipt",
            "Do not substitute arbitrary shell or browser actions for a typed external effector",
        ]
        discord_id = None
        if config.auto_send and config.discord_target.startswith("discord:"):
            discord_id = config.discord_target.split(":", 1)[1]
        job, _ = store.create_hermes_job(
            job_id=job_id,
            run_id=run_id,
            archive_id=archive_id,
            event_id=event_id,
            idempotency_key=idempotency_key,
            capsule_path=str(capsule_path),
            category=category,
            entity=named_entity,
            intent=intent,
            acceptance=acceptance,
            discord_chat_id=discord_id,
            discord_user_id=discord_id,
            initial_state="backlog" if deferred else "pending",
        )
        if deferred:
            await bus.publish(
                "job_backlogged", {"event_id": event_id, "run_id": run_id, "job_id": job["id"]}
            )
            return {
                "status": "backlog",
                "run_id": run_id,
                "job_id": job["id"],
                "orchestrator": "hermes-kanban",
            }
        if not controls.operation_allowed(Capability.ACTIONS) or not controls.operation_allowed(
            Capability.SESSIONS
        ):
            parked = controls.park_run(
                run_id,
                actor="ingestion",
                reason_code="control_plane_paused",
            )
            await bus.publish(
                "job_parked",
                {"event_id": event_id, "run_id": run_id, "job_id": job["id"]},
            )
            return {
                "status": "parked",
                "run_id": run_id,
                "job_id": job["id"],
                "orchestrator": "hermes-kanban",
                "control_receipt_id": parked["receipt_id"],
            }
        await bus.publish(
            "job_queued", {"event_id": event_id, "run_id": run_id, "job_id": job["id"]}
        )
        return {
            "status": "queued",
            "run_id": run_id,
            "job_id": job["id"],
            "orchestrator": "hermes-kanban",
        }

    def start_computer_use(
        task_text: str, *, source_event_id: str | None = None, start_url: str | None = None
    ) -> dict[str, object]:
        session = store.create_computer_session(
            task=task_text,
            agent=config.h_agent,
            source_event_id=source_event_id,
        )
        task = asyncio.create_task(
            h_executor.run(session["id"], task_text, start_url=start_url)
        )
        application.state.background_tasks.add(task)
        task.add_done_callback(application.state.background_tasks.discard)
        return session

    async def ingest(
        payload: TranscriptEventIn,
        *,
        speaker: SpeakerContext,
        suppress_dispatch: bool = False,
        waiting_for_context: bool = False,
    ) -> dict[str, object]:
        transcript = payload.text.strip()
        if len(transcript.encode()) > config.max_transcript_bytes:
            raise HTTPException(status_code=413, detail="transcript exceeds configured limit")
        authorize_capture(f"transcript:{payload.event_id}", f"capture:{payload.source}")
        relevance = evaluate_relevance(
            transcript,
            speaker,
            store.interest_model(),
            # Followthrough currently has no outbound email driver. A sender
            # address alone would not make delivery real, so fail closed until
            # an actual email capability is connected.
            available_capabilities=frozenset(),
        )
        category = (
            relevance.primary_category.value
            if relevance.primary_category
            else relevance.reason_code
        )
        if relevance.dispatch_allowed:
            kind = category
        elif relevance.owner_status == OwnerStatus.NON_OWNER:
            kind = "non_owner_speaker"
        elif relevance.owner_status == OwnerStatus.UNKNOWN:
            kind = "owner_unverified"
        else:
            kind = relevance.reason_code
        dispatch_allowed = relevance.dispatch_allowed and not waiting_for_context
        if waiting_for_context:
            kind = "waiting_for_context"
        classification = Classification(
            dispatch_allowed,
            kind,
            relevance.confidence,
            "waiting_for_context" if kind == "waiting_for_context" else relevance.reason_code,
        )
        transcript_bytes = transcript.encode()
        archived, created = archive_store.archive_event(
            event_id=payload.event_id,
            device_id=payload.device_id,
            source=payload.source,
            occurred_at=_utc(payload.occurred_at),
            transcript_bytes=transcript_bytes,
            transcript_sha256=archive.digest(transcript.encode()),
            relevant=dispatch_allowed,
            classification=classification.kind,
            metadata={
                **payload.metadata,
                "relevance": relevance.to_dict(),
                **({"context_state": "waiting_for_context"} if waiting_for_context else {}),
            },
        )
        if not created:
            if archived["transcript_sha256"] != archive.digest(transcript.encode()):
                raise HTTPException(
                    status_code=409, detail="event ID already exists with different transcript"
                )
            if archived.get("run_id"):
                existing_run = store.get_run(archived["run_id"])
                existing_job = store.hermes_job_for_run(archived["run_id"])
                existing_computer = store.computer_session_for_event(payload.event_id)
                return {
                    "event_id": payload.event_id,
                    "archive_id": archived["id"],
                    "created": False,
                    "status": existing_run["status"] if existing_run else "accepted",
                    "run_id": archived["run_id"],
                    **({"job_id": existing_job["id"]} if existing_job else {}),
                    **(
                        {"computer_use_id": existing_computer["id"]}
                        if existing_computer
                        else {}
                    ),
                }
            return {
                "event_id": payload.event_id,
                "archive_id": archived["id"],
                "created": False,
                "status": (
                    "waiting_for_context"
                    if classification.kind == "waiting_for_context"
                    else "clarification_setup_needed"
                    if relevance.reason_code == "clarification_setup_needed"
                    else "archived"
                ),
                "classification": archived["classification"],
            }
        # Announce before the operations-DB write: if record_relevance fails,
        # the retry takes the created=False path and would never publish, so
        # dashboards would permanently miss a row that is durably archived.
        await bus.publish(
            "transcript_archived",
            {
                "event_id": payload.event_id,
                "device_id": payload.device_id,
                "source": payload.source,
                "occurred_at": archived["occurred_at"],
                "received_at": archived["received_at"],
                "text": transcript,
                # A segment folded into an aggregate is archived but never
                # dispatched itself; the live event must not claim otherwise.
                "relevant": dispatch_allowed and not suppress_dispatch,
                "classification": "aggregate_component" if suppress_dispatch else classification.kind,
                "aggregated": bool(payload.metadata.get("aggregated")),
                "component_event_ids": payload.metadata.get("component_event_ids", []),
                "utterance_id": payload.metadata.get("utterance_id"),
            },
        )
        store.record_relevance(archived["id"], payload.event_id, relevance.to_dict())
        if suppress_dispatch:
            archive_store.set_classification(
                archived["id"], relevant=False, classification="aggregate_component"
            )
            await bus.publish(
                "archive_only",
                {"event_id": payload.event_id, "classification": "aggregate_component"},
            )
            return {
                "event_id": payload.event_id,
                "archive_id": archived["id"],
                "created": True,
                "status": "archived",
                "classification": {
                    **classification.__dict__,
                    "actionable": False,
                    "kind": "aggregate_component",
                    "reason": "Dispatch is owned by the content-addressed aggregate event",
                },
                "relevance": relevance.to_dict(),
                "operational_memory": False,
                "dispatch_suppressed_for_aggregate": True,
            }
        if not dispatch_allowed:
            await bus.publish(
                "archive_only",
                {"event_id": payload.event_id, "classification": classification.kind},
            )
            return {
                "event_id": payload.event_id,
                "archive_id": archived["id"],
                "created": True,
                "status": (
                    "waiting_for_context"
                    if classification.kind == "waiting_for_context"
                    else "clarification_setup_needed"
                    if relevance.reason_code == "clarification_setup_needed"
                    else "archived"
                ),
                "classification": classification.__dict__,
                "relevance": relevance.to_dict(),
                "operational_memory": False,
            }
        run_id = store.create_run(
            payload.source, classification.kind, "Followthrough signal", archived["id"]
        )
        archive_store.link_run(archived["id"], run_id)
        subject = operational_entity(transcript, category)
        store.add_operational_memory(
            archived["id"], run_id, relevance.content_fingerprint, category, subject
        )
        queued = await enqueue_durable_job(
            run_id,
            archived["id"],
            payload.event_id,
            category,
            subject,
            deferred=(
                relevance.reason_code != "owner_explicit_memo_command"
                and category in {"tool", "startup", "goal"}
                and relevance.confidence < 0.93
            ),
        )
        computer_use = None
        if category == "web_task":
            computer_use = start_computer_use(
                subject,
                source_event_id=payload.event_id,
                # Land the agent on the named site directly; the search-engine
                # hop is where most steps and bot-checks were spent. Derive it
                # from the cleaned command, or the wake word and the words meant
                # for Followthrough end up inside the site's search box.
                start_url=payload.metadata.get("start_url") or start_url(subject),
            )
        return {
            "event_id": payload.event_id,
            "archive_id": archived["id"],
            "created": True,
            "classification": classification.__dict__,
            "relevance": relevance.to_dict(),
            **queued,
            **({"computer_use_id": computer_use["id"]} if computer_use else {}),
        }

    @application.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (static / "index.html").read_text()

    @application.get("/healthz")
    async def healthz() -> dict[str, object]:
        database_ready = bool(store.db.execute("SELECT 1").fetchone()[0])
        archive_ready = archive_store.integrity_check()
        orchestrator = store.heartbeat_status("orchestrator") if config.kanban_enabled else None
        return {
            "ok": database_ready and archive_ready,
            "service": "followthrough",
            "version": "0.2.0",
            "database_ready": database_ready,
            "archive_ready": archive_ready,
            "auth_required": False,
            "h_company_configured": h_executor.configured,
            "hermes_cli_present": shutil.which(config.hermes_bin) is not None,
            "orchestrator": orchestrator,
            "job_counts": store.hermes_job_counts(),
            "control_mode": controls.status()["global"]["mode"],
            "livekit_configured": bool(
                config.livekit_url and config.livekit_api_key and config.livekit_api_secret
            ),
        }

    @application.post("/api/v1/livekit/session")
    async def livekit_memo_session(payload: LiveKitSessionIn) -> dict[str, str]:
        if not payload.consent:
            raise HTTPException(status_code=400, detail="explicit capture consent is required")
        if not payload.device_id:
            raise HTTPException(status_code=422, detail="Memo device_id is required")
        authorize_capture(f"livekit-session:{payload.device_id}", "capture:livekit")
        try:
            issued = issue_memo_session_token(
                server_url=config.livekit_url,
                api_key=config.livekit_api_key,
                api_secret=config.livekit_api_secret,
                agent_name=config.livekit_agent_name,
                device_id=payload.device_id,
                surface=payload.surface,
                response_mode=payload.response_mode,
                ttl_seconds=config.livekit_token_ttl_seconds,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "server_url": issued.server_url,
            "participant_token": issued.participant_token,
            "room_name": issued.room_name,
            "participant_identity": issued.participant_identity,
        }

    @application.post("/api/v1/transcripts", status_code=202)
    async def transcript_event(payload: TranscriptEventIn) -> dict[str, object]:
        if not payload.consent:
            raise HTTPException(status_code=400, detail="capture consent flag is required")
        speaker = SpeakerContext.native_owner(payload.device_id)
        if payload.metadata.get("aggregated"):
            for component_event_id in payload.metadata.get("component_event_ids", []):
                component = archive_store.by_event(str(component_event_id))
                if component:
                    archive_store.set_classification(
                        component["id"], relevant=False, classification="aggregate_component"
                    )
        aggregate = None
        aggregator = None
        already_archived = archive_store.by_event(payload.event_id) is not None
        if (
            already_archived
            and payload.source in {"phone", "wearable"}
            and not payload.metadata.get("aggregated")
        ):
            # Reconstruct only a self-contained one-segment aggregate on replay.
            # This returns the original aggregate's run/job receipt without
            # mixing newly buffered ambient context into a second aggregate.
            replay_aggregator = TranscriptAggregator()
            aggregate = replay_aggregator.add(
                Transcript(payload.event_id, _utc(payload.occurred_at), payload.text)
            )
            aggregator = replay_aggregator
        elif payload.source in {"phone", "wearable"} and not payload.metadata.get("aggregated"):
            aggregator = application.state.transcript_aggregators.setdefault(
                payload.device_id, TranscriptAggregator()
            )
            aggregate = aggregator.add(
                Transcript(payload.event_id, _utc(payload.occurred_at), payload.text)
            )
        result = await ingest(
            payload,
            speaker=speaker,
            suppress_dispatch=aggregate is not None,
            waiting_for_context=(
                bool(aggregator and aggregator.waiting_for_context) and aggregate is None
            ),
        )
        if aggregate and aggregator:
            # The complete utterance replaces its ASR-sized component rows in
            # the user-facing transcript. The immutable component text stays
            # in the archive, but it must never look like separate speech.
            for component_event_id in aggregate.component_event_ids:
                component = archive_store.by_event(component_event_id)
                if component:
                    archive_store.set_classification(
                        component["id"], relevant=False, classification="aggregate_component"
                    )
            aggregate_payload = TranscriptEventIn(
                event_id=aggregate.event_id,
                device_id=payload.device_id,
                text=aggregate.text,
                source=payload.source,
                occurred_at=datetime.fromisoformat(aggregate.occurred_at),
                consent=True,
                metadata={
                    **payload.metadata,
                    "aggregated": True,
                    "component_event_ids": list(aggregate.component_event_ids),
                    "aggregate_window_seconds": aggregator.window_seconds,
                },
            )
            aggregate_result = await ingest(aggregate_payload, speaker=speaker)
            return {
                **aggregate_result,
                "aggregate_event_id": aggregate.event_id,
                "original_event_id": payload.event_id,
            }
        return result

    @application.post("/api/v1/transcripts/partial", status_code=202)
    async def transcript_partial(payload: TranscriptPartialIn) -> dict[str, object]:
        """Fan an in-flight ASR hypothesis out to live dashboards.

        Partials are ephemeral by design: they never touch the archive, the
        relevance gate, or Hermes. The finalized utterance arrives separately
        through the normal transcript ingestion path.
        """
        if not payload.consent:
            raise HTTPException(status_code=400, detail="capture consent flag is required")
        if len(payload.text.encode()) > config.max_transcript_bytes:
            raise HTTPException(status_code=413, detail="transcript exceeds configured limit")
        if not controls.operation_allowed(Capability.LISTENING):
            raise HTTPException(status_code=503, detail="listening is paused")
        await bus.publish(
            "transcript_partial",
            {
                "utterance_id": payload.utterance_id,
                "device_id": payload.device_id,
                "source": payload.source,
                "seq": payload.seq,
                "text": payload.text,
                "occurred_at": _utc(payload.occurred_at),
                "received_at": _utc(),
            },
        )
        return {
            "utterance_id": payload.utterance_id,
            "seq": payload.seq,
            "status": "streamed",
            "archived": False,
        }

    @application.get("/api/transcript")
    async def transcript_feed(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None, max_length=64),
        before_id: str | None = Query(default=None, max_length=64),
    ) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for event in archive_store.recent_transcripts(limit, before, before_id):
            entries.append(
                {
                    "event_id": event["event_id"],
                    "archive_id": event["id"],
                    "device_id": event["device_id"],
                    "source": event["source"],
                    "occurred_at": event["occurred_at"],
                    "received_at": event["received_at"],
                    "text": event["transcript_bytes"].decode("utf-8", errors="replace"),
                    "relevant": bool(event["relevant"]),
                    "classification": event["classification"],
                }
            )
        return entries

    @application.get("/api/v1/audio/{event_id}/status")
    async def audio_status(event_id: str) -> dict[str, object]:
        archived = archive_store.by_event(event_id)
        if not archived:
            raise HTTPException(status_code=404, detail="archive event not found")
        return {"event_id": event_id, **archive_store.audio_manifest(archived["id"])}

    @application.put("/api/v1/audio/{event_id}/{sequence}")
    async def audio_chunk(
        event_id: str,
        sequence: int,
        request: Request,
        x_device_id: str | None = Header(default=None),
        x_content_sha256: str | None = Header(default=None),
        content_type: str | None = Header(default="application/octet-stream"),
    ) -> dict[str, object]:
        if sequence < 0:
            raise HTTPException(status_code=422, detail="sequence must be non-negative")
        if sequence > config.max_audio_sequence:
            raise HTTPException(status_code=422, detail="sequence exceeds configured maximum")
        authorize_capture(f"audio:{event_id}:{sequence}", "capture:native-audio")
        archived = archive_store.by_event(event_id)
        if not archived:
            raise HTTPException(
                status_code=404, detail="transcript event must be ingested before audio"
            )
        if x_device_id and x_device_id != archived["device_id"]:
            raise HTTPException(status_code=403, detail="device does not own this event")
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > config.max_audio_chunk_bytes:
            raise HTTPException(status_code=413, detail="audio chunk exceeds configured limit")
        body = await request.body()
        if not body:
            raise HTTPException(status_code=422, detail="empty audio chunk")
        if len(body) > config.max_audio_chunk_bytes:
            raise HTTPException(status_code=413, detail="audio chunk exceeds configured limit")
        digest = archive.digest(body)
        if x_content_sha256 and x_content_sha256.lower() != digest:
            raise HTTPException(status_code=422, detail="audio digest mismatch")
        mime = content_type or "application/octet-stream"
        try:
            chunk, created = archive_store.persist_audio_chunk(
                archived["id"],
                sequence,
                mime,
                digest,
                len(body),
                lambda: archive.write_audio(archived["id"], sequence, body),
            )
        except OSError as exc:
            raise HTTPException(status_code=503, detail="archive storage unavailable") from exc
        if chunk["plaintext_sha256"] != digest:
            raise HTTPException(
                status_code=409, detail="audio sequence already exists with different content"
            )
        return {
            "event_id": event_id,
            "sequence": sequence,
            "plaintext_sha256": digest,
            "plaintext_bytes": len(body),
            "created": created,
        }

    @application.post("/api/signals")
    async def signal(
        payload: SignalIn) -> dict[str, object]:
        if not payload.consent:
            raise HTTPException(status_code=400, detail="explicit consent is required")
        event = TranscriptEventIn(
            event_id="web:" + str(uuid.uuid4()),
            device_id="dashboard",
            text=payload.text,
            source=payload.source,
            consent=True,
            metadata={
                "email": str(payload.email) if payload.email else None,
                "allow_owner_report": payload.allow_owner_report,
                **({"utterance_id": payload.utterance_id} if payload.utterance_id else {}),
            },
        )
        if payload.email:
            store.activate(str(payload.email))
        return await ingest(
            event,
            speaker=SpeakerContext.native_owner("dashboard"),
        )

    @application.post("/api/webhooks/omi/transcript", status_code=202)
    async def omi_transcript(
        request: Request,
        uid: str = Query(min_length=1, max_length=200),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        payload = await request.json()
        events = _omi_segment_events(payload, uid, idempotency_key, "transcript")
        receipts = [
            await ingest(
                event,
                speaker=SpeakerContext.omi(
                    is_user=is_user,
                    ambient_authorized=True,
                ),
            )
            for event, is_user in events
        ]
        return {"accepted": len(receipts), "receipts": receipts}

    @application.post("/api/webhooks/omi/conversation", status_code=202)
    async def omi_conversation(
        request: Request,
        uid: str = Query(min_length=1, max_length=200),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        payload = await request.json()
        events = _omi_segment_events(payload, uid, idempotency_key, "conversation")
        receipts = [
            await ingest(
                event,
                speaker=SpeakerContext.omi(
                    is_user=is_user,
                    ambient_authorized=True,
                ),
            )
            for event, is_user in events
        ]
        return {"accepted": len(receipts), "receipts": receipts}

    @application.post("/api/webhooks/omi/audio", status_code=202)
    async def omi_audio(
        request: Request,
        uid: str = Query(min_length=1, max_length=200),
        sample_rate: int = Query(default=16_000, ge=8_000, le=96_000),
        timestamp: str | None = Query(default=None, max_length=100),
        sequence: str | None = Query(default=None, max_length=100),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        content_type: str | None = Header(default=None),
    ) -> dict[str, object]:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > config.max_audio_chunk_bytes:
            raise HTTPException(status_code=413, detail="audio chunk exceeds configured limit")
        body = await request.body()
        if not body:
            raise HTTPException(status_code=422, detail="empty audio chunk")
        if len(body) > config.max_audio_chunk_bytes:
            raise HTTPException(status_code=413, detail="audio chunk exceeds configured limit")
        digest = archive.digest(body)
        idempotency_source = "explicit"
        if not idempotency_key:
            # Current official Omi sends only uid+sample_rate. Content hashes
            # cannot be used as delivery identity because consecutive 5-second
            # silence chunks are legitimately identical. Preserve every chunk
            # unless Omi supplies a timestamp/sequence that can identify a
            # retry without collapsing real audio.
            stable_delivery_identity = bool(timestamp or sequence)
            idempotency_source = (
                "official_omi_derived"
                if stable_delivery_identity
                else "official_omi_unique_delivery"
            )
            idempotency_key = _omi_audio_delivery_key(
                device_identity=hashlib.sha256(uid.encode()).hexdigest(),
                uid=uid,
                sample_rate=sample_rate,
                payload_digest=digest,
                timestamp=timestamp,
                sequence=sequence,
                delivery_nonce=None if stable_delivery_identity else uuid.uuid4().hex,
            )
        event_id = f"omi:audio:{uid}:{idempotency_key}"
        authorize_capture(f"audio:{event_id}:0", "capture:omi-audio")
        existing_event = archive_store.by_event(event_id)
        if existing_event:
            existing_chunk = archive_store.audio_chunk(existing_event["id"], 0)
            if existing_chunk and existing_chunk["plaintext_sha256"] != digest:
                raise HTTPException(
                    status_code=409, detail="Omi audio idempotency key reused with different bytes"
                )
            if existing_chunk:
                return {
                    "event_id": event_id,
                    "created": False,
                    "plaintext_sha256": digest,
                    "plaintext_bytes": len(body),
                    "sample_rate": sample_rate,
                }
            archived, created_event = existing_event, False
        else:
            received_at = datetime.now(UTC)
            stream_id = f"omi:{uid}:{received_at.date().isoformat()}"
            stream_sequence = archive_store.allocate_stream_sequence(stream_id, uid, "omi")
            mime = (content_type or "application/octet-stream").split(";", 1)[0].lower()
            pcm_delivery = mime in {"application/octet-stream", "audio/l16", "audio/pcm"}
            duration_ms = round(len(body) / (sample_rate * 2) * 1000) if pcm_delivery else None
            empty = b""
            archived, created_event = archive_store.archive_event(
                event_id=event_id,
                device_id=uid,
                source="omi",
                occurred_at=_utc(received_at),
                transcript_bytes=empty,
                transcript_sha256=archive.digest(empty),
                relevant=False,
                classification="audio_only",
                metadata={
                    "sample_rate": sample_rate if pcm_delivery else None,
                    "encoding": "pcm_s16le_mono" if pcm_delivery else mime,
                    "idempotency_key": idempotency_key,
                    "idempotency_source": idempotency_source,
                    "omi_timestamp": timestamp,
                    "omi_sequence": sequence,
                    "capture_stream_id": stream_id,
                    "stream_sequence": stream_sequence,
                    "duration_ms": duration_ms,
                    "alignment": "arrival_time_estimate",
                },
            )
        existing = archive_store.audio_chunk(archived["id"], 0)
        if existing:
            if existing["plaintext_sha256"] != digest:
                raise HTTPException(
                    status_code=409, detail="Omi audio idempotency key reused with different bytes"
                )
            return {
                "event_id": event_id,
                "created": False,
                "plaintext_sha256": digest,
                "plaintext_bytes": len(body),
            }
        mime = (content_type or "application/octet-stream").split(";", 1)[0].lower()
        if mime == "application/octet-stream":
            mime = "audio/L16"
        chunk, created_chunk = archive_store.persist_audio_chunk(
            archived["id"],
            0,
            mime,
            digest,
            len(body),
            lambda: archive.write_audio(archived["id"], 0, body),
        )
        if chunk["plaintext_sha256"] != digest:
            raise HTTPException(
                status_code=409, detail="Omi audio idempotency key reused with different bytes"
            )
        return {
            "event_id": event_id,
            "created": created_chunk,
            "archive_created": created_event,
            "recovered_incomplete_event": not created_event and created_chunk,
            "plaintext_sha256": digest,
            "plaintext_bytes": len(body),
            "sample_rate": sample_rate,
        }

    @application.post("/api/webhooks/omi")
    async def omi(
        request: Request,
        x_device_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Omi payload must be a JSON object")
        is_user = payload.get("is_user") if isinstance(payload.get("is_user"), bool) else None
        return await ingest(
            _omi_event(payload, x_device_id),
            speaker=SpeakerContext.omi(
                is_user=is_user,
                ambient_authorized=True,
            ),
        )

    @application.get("/api/relevance/{event_id}")
    async def relevance_decision(
        event_id: str
    ) -> dict[str, object]:
        decision = store.relevance_for_event(event_id)
        if not decision:
            raise HTTPException(status_code=404, detail="relevance decision not found")
        return decision

    @application.post("/api/relevance/interests")
    async def set_interest(
        payload: InterestWeightIn
    ) -> dict[str, object]:
        weight = InterestWeight(payload.category, payload.weight, payload.source)
        store.set_interest_weight(weight)
        return weight.to_dict()

    @application.post("/api/relevance/corrections", status_code=202)
    async def correct_relevance(
        payload: RelevanceCorrectionIn
    ) -> dict[str, object]:
        archived = archive_store.by_event(payload.event_id)
        decision = store.relevance_for_event(payload.event_id)
        if not archived or not decision:
            raise HTTPException(
                status_code=404, detail="archive event or relevance decision not found"
            )
        categories = tuple(payload.categories) or tuple(
            Category(value) for value in decision["categories"] if value != "ordinary_life"
        )
        correction = CorrectionRecord(
            content_fingerprint=decision["content_fingerprint"],
            disposition=payload.disposition,
            categories=categories,
            reason_code=payload.reason_code,
        )
        store.add_relevance_correction(correction)
        owner_status = OwnerStatus(decision["owner_status"])
        if archived["source"] == "omi":
            try:
                archive_metadata = json.loads(archived["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                archive_metadata = {}
            original_is_user = archive_metadata.get("is_user")
            speaker = SpeakerContext.omi(
                is_user=original_is_user if isinstance(original_is_user, bool) else None,
                ambient_authorized=True,
            )
        elif owner_status == OwnerStatus.OWNER:
            speaker = SpeakerContext.native_owner("verified-correction")
        elif owner_status == OwnerStatus.NON_OWNER:
            speaker = SpeakerContext.native_non_owner("verified-correction")
        else:
            speaker = SpeakerContext.unknown()
        try:
            transcript = archived["transcript_bytes"].decode()
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=503, detail="archive record is unavailable") from exc
        result = evaluate_relevance(transcript, speaker, store.interest_model())
        category = result.primary_category.value if result.primary_category else result.reason_code
        classification = Classification(
            result.dispatch_allowed,
            category if result.dispatch_allowed else result.reason_code,
            result.confidence,
            result.reason_code,
        )
        archive_store.set_classification(
            archived["id"], relevant=result.dispatch_allowed, classification=classification.kind
        )
        store.record_relevance(archived["id"], payload.event_id, result.to_dict())
        run_id = archived.get("run_id")
        cancelled = False
        if result.dispatch_allowed and not run_id:
            run_id = store.create_run(
                archived["source"],
                classification.kind,
                "Corrected Followthrough signal",
                archived["id"],
            )
            archive_store.link_run(archived["id"], run_id)
            subject = operational_entity(transcript, category)
            store.add_operational_memory(
                archived["id"], run_id, result.content_fingerprint, category, subject
            )
            await enqueue_durable_job(
                run_id, archived["id"], payload.event_id, category, subject
            )
        elif not result.dispatch_allowed and run_id:
            cancelled = store.cancel_nonrunning_hermes_job(
                run_id,
                reason=f"relevance_correction:{payload.reason_code}",
            )
        return {
            "event_id": payload.event_id,
            "run_id": run_id,
            "relevance": result.to_dict(),
            "correction": correction.to_dict(),
            "cancelled_nonrunning_job": cancelled,
        }

    @application.get("/api/memory/operational")
    async def operational_memory(
    ) -> list[dict[str, object]]:
        return store.list_operational_memories()

    @application.get("/api/jobs")
    async def jobs() -> list[dict[str, object]]:
        return store.list_hermes_jobs()

    @application.get("/api/workspace")
    async def workspace_items() -> list[dict[str, object]]:
        return store.list_workspace_items()

    @application.patch("/api/workspace/{item_id}")
    async def update_workspace_item(
        item_id: str, payload: WorkspaceItemIn
    ) -> dict[str, object]:
        item = store.update_workspace_item(item_id, title=payload.title.strip(), group=payload.group)
        if not item:
            raise HTTPException(status_code=404, detail="workspace item not found")
        await bus.publish("workspace_updated", {"item_id": item_id})
        return item

    @application.delete("/api/workspace/{item_id}", status_code=204)
    async def delete_workspace_item(item_id: str) -> Response:
        if not store.delete_workspace_item(item_id):
            raise HTTPException(status_code=404, detail="workspace item not found")
        await bus.publish("workspace_updated", {"item_id": item_id, "deleted": True})
        return Response(status_code=204)

    @application.get("/api/v1/jobs/{job_id}")
    async def device_job(job_id: str) -> dict[str, object]:
        job = store.hermes_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        store.mark_job_polled(job["id"])
        run = store.get_run(job["run_id"])
        summary = (run or {}).get("summary") or job.get("latest_outcome")
        # A web task is executed by the computer-use agent, not by the research
        # worker. Its answer is the ground truth for this job, so it wins over a
        # worker summary that could only guess at the live page.
        session = store.computer_session_for_event(job["event_id"])
        state = job["state"]
        error = job.get("last_error")
        if session:
            if session.get("state") == "completed" and session.get("latest_answer"):
                summary = session["latest_answer"]
            elif session["state"] not in H_TERMINAL_STATES:
                # The agent that owns this task is still working. Report it as
                # in progress rather than speaking the research worker's guess.
                state = "in_progress"
                summary = None
            else:
                # The typed browser runner owns web-task truth. Never report a
                # research worker's fallback as success when H failed.
                state = str(session["state"])
                summary = None
                error = session.get("error") or f"H Company session {state}"
        return {
            "job_id": job["id"],
            "run_id": job["run_id"],
            "task_id": job.get("task_id"),
            "state": state,
            "category": job.get("category"),
            "entity": job.get("entity"),
            "summary": summary,
            "error": error,
            "computer_use": (
                {
                    "id": session["id"],
                    "state": session["state"],
                    "steps": session.get("step_count"),
                    "agent_view_url": session.get("agent_view_url"),
                }
                if session
                else None
            ),
            "updated_at": job["updated_at"],
        }

    @application.get("/api/controls")
    async def control_status() -> dict[str, object]:
        return controls.status()

    @application.get("/api/controls/audit")
    async def control_audit(
    ) -> list[dict[str, object]]:
        return controls.audit_log()

    @application.post("/api/controls/global")
    async def change_global_control(
        payload: GlobalControlIn
    ) -> dict[str, object]:
        return controls.set_global_mode(
            payload.mode,
            actor=payload.actor,
            reason_code=payload.reason_code,
            resume_parked=payload.resume_parked,
        )

    @application.post("/api/controls/safe-mode")
    async def activate_safe_mode(
        payload: SafeModeIn
    ) -> dict[str, object]:
        return controls.trigger_safe_mode(payload.trigger, actor=payload.actor)

    @application.post("/api/controls/capabilities/{capability}")
    async def change_capability(
        capability: str,
        payload: CapabilityControlIn,
    ) -> dict[str, object]:
        try:
            return controls.set_capability(
                capability,
                payload.enabled,
                actor=payload.actor,
                reason_code=payload.reason_code,
                resume_parked=payload.resume_parked,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.put("/api/controls/limits/{capability}")
    async def change_capability_limit(
        capability: str,
        payload: CapabilityLimitIn,
    ) -> dict[str, object]:
        try:
            return controls.set_limit(
                capability,
                max_events=payload.max_events,
                window_seconds=payload.window_seconds,
                max_cost_usd=payload.max_cost_usd,
                actor=payload.actor,
                reason_code=payload.reason_code,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/controls/jobs/{run_id}/park")
    async def park_job(
        run_id: str, payload: TaskControlIn
    ) -> dict[str, object]:
        return controls.park_run(run_id, actor=payload.actor, reason_code=payload.reason_code)

    @application.post("/api/controls/jobs/resume")
    async def resume_jobs(
        payload: TaskControlIn
    ) -> dict[str, object]:
        return controls.resume_parked(actor=payload.actor, reason_code=payload.reason_code)

    @application.get("/api/runs")
    async def runs() -> list[dict[str, object]]:
        return store.list_runs()

    @application.get("/api/runs/{run_id}")
    async def run(
        run_id: str
    ) -> dict[str, object]:
        found = store.get_run(run_id)
        if not found:
            raise HTTPException(status_code=404, detail="run not found")
        return found

    @application.get("/api/metrics")
    async def metrics() -> dict[str, object]:
        return {
            **store.metrics(),
            **archive_store.metrics(),
            "job_counts": store.hermes_job_counts(),
            "orchestrator": store.heartbeat_status("orchestrator"),
            "integrations": {
                "hermes": shutil.which(config.hermes_bin) is not None,
                "h_company": h_executor.configured,
                "orgo_remote": bool(
                    config.orgo_api_key and config.orgo_default_computer_id
                ),
                "spark_local": bool(desktop.local_token),
            },
        }

    @application.get("/api/journey")
    async def journey() -> dict[str, object]:
        """Return one event-linked story for the judge-facing live timeline.

        The dashboard previously guessed relationships by independently sorting
        activity, jobs, and H sessions. This contract follows one event ID all
        the way through relevance, execution, Discord, and phone polling so a
        busy system cannot splice together unrelated runs.
        """

        sessions = store.list_computer_sessions(limit=1)
        session = sessions[0] if sessions else None
        job = (
            store.hermes_job_for_event(str(session["source_event_id"]))
            if session and session.get("source_event_id")
            else None
        )
        if not job:
            jobs = store.list_hermes_jobs(limit=1)
            job = jobs[0] if jobs else None
        event_id = str(
            (session or {}).get("source_event_id") or (job or {}).get("event_id") or ""
        )
        if not event_id:
            return {"event_id": None, "stages": [], "state": "idle"}
        if not session or session.get("source_event_id") != event_id:
            session = store.computer_session_for_event(event_id)
        archived = archive_store.by_event(event_id)
        decision = store.relevance_for_event(event_id)
        transcript = ""
        if archived:
            transcript = archived["transcript_bytes"].decode("utf-8", errors="replace")[:320]

        h_state = str((session or {}).get("state") or "pending")
        h_terminal = h_state in H_TERMINAL_STATES
        verified = bool(
            session
            and h_state == "completed"
            and str(session.get("latest_answer") or "").strip()
        )
        discord_delivered = bool(job and job.get("notification_state") == "delivered")
        returned_at = (job or {}).get("last_polled_at")
        returned = bool(returned_at)
        failed = h_terminal and not verified

        def stage(
            key: str,
            label: str,
            state: str,
            at: object = None,
            detail: str = "",
        ) -> dict[str, object]:
            return {"key": key, "label": label, "state": state, "at": at, "detail": detail}

        explanation = ""
        if decision:
            evidence = decision.get("evidence") or []
            explanation = next(
                (
                    str(item.get("explanation"))
                    for item in reversed(evidence)
                    if isinstance(item, dict) and item.get("explanation")
                ),
                str(decision.get("reason_code") or ""),
            )
        stages = [
            stage(
                "heard",
                "Heard",
                "done" if archived else "pending",
                (archived or {}).get("received_at"),
                f"{(archived or {}).get('source', 'phone')} transcript finalized",
            ),
            stage(
                "relevant",
                "Relevant",
                "done" if decision and decision.get("disposition") == "action" else "failed",
                (decision or {}).get("created_at"),
                explanation,
            ),
            stage(
                "delegated",
                "Delegated",
                "done" if job else "pending",
                (job or {}).get("created_at"),
                f"Hermes {(job or {}).get('task_id') or 'receipt pending'}",
            ),
            stage(
                "browsing",
                "Browsing",
                "failed" if failed else "done" if verified else "active" if session else "pending",
                (session or {}).get("created_at"),
                f"{(session or {}).get('step_count', 0)} H Company steps",
            ),
            stage(
                "verified",
                "Verified",
                "failed" if failed else "done" if verified else "pending",
                (session or {}).get("finished_at"),
                "H answer received" if verified else str((session or {}).get("error") or ""),
            ),
            stage(
                "discord",
                "Discord",
                "done" if discord_delivered else "active" if verified else "pending",
                (job or {}).get("updated_at") if discord_delivered else None,
                "Owner DM delivered" if discord_delivered else "Awaiting typed delivery",
            ),
            stage(
                "phone",
                "Phone",
                "done" if returned else "active" if verified else "pending",
                returned_at,
                "Result collected by Memo" if returned else "Awaiting Memo poll",
            ),
        ]
        started_at = (archived or {}).get("received_at") or (session or {}).get("created_at")
        ended_at = returned_at or (session or {}).get("finished_at") or (session or {}).get("updated_at")
        elapsed = None
        if started_at and ended_at:
            elapsed = max(
                0,
                round(
                    (
                        datetime.fromisoformat(str(ended_at))
                        - datetime.fromisoformat(str(started_at))
                    ).total_seconds()
                ),
            )
        execution_seconds = None
        if (session or {}).get("created_at") and (session or {}).get("finished_at"):
            execution_seconds = max(
                0,
                round(
                    (
                        datetime.fromisoformat(str(session["finished_at"]))
                        - datetime.fromisoformat(str(session["created_at"]))
                    ).total_seconds()
                ),
            )
        return {
            "event_id": event_id,
            "state": "failed" if failed else "completed" if returned and discord_delivered else "running",
            "transcript": transcript,
            "source": (archived or {}).get("source"),
            "decision": (
                {
                    "category": ((decision or {}).get("categories") or [None])[0],
                    "confidence": (decision or {}).get("confidence"),
                    "reason_code": (decision or {}).get("reason_code"),
                    "explanation": explanation,
                }
                if decision
                else None
            ),
            "job": (
                {
                    "id": job["id"],
                    "state": job["state"],
                    "task_id": job.get("task_id"),
                    "entity": job.get("entity"),
                }
                if job
                else None
            ),
            "computer_use": session,
            "discord_delivered": discord_delivered,
            "phone_returned": returned,
            "elapsed_seconds": elapsed,
            "execution_seconds": execution_seconds,
            "stages": stages,
        }

    @application.get("/api/activity")
    async def activity(
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for event in archive_store.recent_events(24):
            try:
                plaintext = event["transcript_bytes"].decode("utf-8", errors="replace")
            except UnicodeDecodeError:
                plaintext = "[transcript unavailable]"
            result.append(
                {
                    "event_id": event["event_id"],
                    "source": event["source"],
                    "device_id": event["device_id"],
                    "occurred_at": event["occurred_at"],
                    "received_at": event["received_at"],
                    "text": plaintext[:320],
                    "relevant": bool(event["relevant"]),
                    "classification": event["classification"],
                    "run_id": event["run_id"],
                }
            )
        return result

    @application.post("/api/computer-use", status_code=202)
    async def launch_computer_use(payload: ComputerUseIn) -> dict[str, object]:
        return start_computer_use(
            payload.task,
            source_event_id=payload.source_event_id,
            start_url=payload.start_url,
        )

    @application.get("/api/computer-use")
    async def computer_use_sessions() -> list[dict[str, object]]:
        return store.list_computer_sessions()

    @application.get("/api/computer-use/{identifier}/frame")
    async def computer_use_frame(identifier: str) -> Response:
        """Stream the agent's own browser screenshot.

        H serves trajectory frames behind the API key, so the browser cannot
        load them directly; proxy the latest one instead of leaking the key.
        """
        session = store.computer_session(identifier)
        if not session or not session.get("latest_frame_url"):
            raise HTTPException(status_code=404, detail="no agent frame yet")
        source = str(session["latest_frame_url"])
        if not source.startswith(config.h_api_base.split("/api/")[0]):
            raise HTTPException(status_code=404, detail="no agent frame yet")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(
                    source, headers={"Authorization": f"Bearer {config.h_api_key.strip()}"}
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="agent frame unavailable") from exc
        return Response(
            content=response.content,
            media_type=response.headers.get("content-type", "image/png"),
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/api/computer-use/{identifier}")
    async def computer_use_session(identifier: str) -> dict[str, object]:
        found = store.computer_session(identifier)
        if not found:
            raise HTTPException(status_code=404, detail="computer-use session not found")
        return found

    @application.post("/api/computer-use/{identifier}/cancel")
    async def cancel_computer_use(identifier: str) -> dict[str, object]:
        try:
            return await h_executor.cancel(identifier)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="computer-use session not found") from exc

    @application.get("/api/desktop/doctor")
    async def desktop_doctor(computer_id: str | None = Query(default=None)) -> dict[str, object]:
        return await desktop.doctor(computer_id)

    @application.get("/api/desktop/screenshot")
    async def desktop_screenshot(computer_id: str | None = Query(default=None)) -> Response:
        try:
            png, metadata = await desktop.screenshot(computer_id)
        except DesktopError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            png,
            media_type="image/png",
            headers={
                "X-Desktop-Provider": str(metadata["provider"]),
                "X-Desktop-Fingerprint": str(metadata["fingerprint"]),
                "Cache-Control": "no-store",
            },
        )

    async def publish_desktop(receipt: dict[str, object]) -> dict[str, object]:
        await bus.publish("desktop_action", receipt)
        return receipt

    @application.post("/api/desktop/click")
    async def desktop_click(payload: DesktopClickIn) -> dict[str, object]:
        try:
            return await publish_desktop(await desktop.click(**payload.model_dump()))
        except DesktopError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post("/api/desktop/drag")
    async def desktop_drag(payload: DesktopDragIn) -> dict[str, object]:
        try:
            return await publish_desktop(await desktop.drag(**payload.model_dump()))
        except DesktopError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post("/api/desktop/type")
    async def desktop_type(payload: DesktopTypeIn) -> dict[str, object]:
        try:
            return await publish_desktop(
                await desktop.type_text(
                    payload.text,
                    delay_ms=payload.delay_ms,
                    computer_id=payload.computer_id,
                    verify=payload.verify,
                )
            )
        except DesktopError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post("/api/desktop/key")
    async def desktop_key(payload: DesktopKeyIn) -> dict[str, object]:
        try:
            return await publish_desktop(await desktop.key(**payload.model_dump()))
        except DesktopError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post("/api/desktop/scroll")
    async def desktop_scroll(payload: DesktopScrollIn) -> dict[str, object]:
        try:
            return await publish_desktop(await desktop.scroll(**payload.model_dump()))
        except DesktopError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post("/api/desktop/lifecycle")
    async def desktop_lifecycle(payload: DesktopLifecycleIn) -> dict[str, object]:
        try:
            return await publish_desktop(await desktop.lifecycle(**payload.model_dump()))
        except DesktopError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/api/desktop/actions")
    async def desktop_actions() -> list[dict[str, object]]:
        return store.list_desktop_actions()

    @application.websocket("/api/desktop/vnc")
    async def desktop_vnc(websocket: WebSocket) -> None:
        """Bridge local-only VNC to the same-origin noVNC client."""
        await websocket.accept()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 5901)
        except OSError:
            await websocket.close(code=1013, reason="local desktop unavailable")
            return

        async def browser_to_vnc() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                payload = message.get("bytes")
                if payload is None and message.get("text") is not None:
                    payload = message["text"].encode("latin-1")
                if payload:
                    writer.write(payload)
                    await writer.drain()

        async def vnc_to_browser() -> None:
            while data := await reader.read(65536):
                await websocket.send_bytes(data)

        tasks = [asyncio.create_task(browser_to_vnc()), asyncio.create_task(vnc_to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            writer.close()
            await writer.wait_closed()

    @application.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(bus.stream(), media_type="text/event-stream")

    return application


app = create_app()
