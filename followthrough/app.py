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

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .adb_bridge import Transcript, TranscriptAggregator
from .archive import ArchiveIntegrityError, ArchiveVault
from .archive_store import ArchiveStore
from .bus import bus
from .classifier import Classification
from .config import Settings, settings
from .controls import Capability, ControlPlane
from .crew import Crew
from .integrations import operational_entity
from .models import (
    CapabilityControlIn,
    CapabilityLimitIn,
    GlobalControlIn,
    ImprovementEvaluationIn,
    ImprovementPolicyIn,
    ImprovementPromotionIn,
    ImprovementProposalIn,
    InterestWeightIn,
    RelevanceCorrectionIn,
    RoleIn,
    SafeModeIn,
    SignalIn,
    SignupIn,
    TaskControlIn,
    TranscriptEventIn,
)
from .relevance import Category, CorrectionRecord, InterestWeight, OwnerStatus, SpeakerContext, evaluate_relevance
from .security import TokenAuthority, bearer_token
from .self_improvement import EvalCaseResult, ImprovementManager
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

    The caller must authenticate the device token before passing its one-way
    identity. The payload is bounded by ``max_audio_chunk_bytes`` before its
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
        stable = json.dumps({"device": device_id, "occurred": occurred, "text": text}, sort_keys=True).encode()
        event_id = "omi:" + hashlib.sha256(stable).hexdigest()
    metadata = {
        key: payload[key]
        for key in ("session_id", "language", "speaker", "speaker_id", "is_user", "app_id")
        if key in payload
    }
    try:
        return TranscriptEventIn(event_id=str(event_id), device_id=device_id, text=text, source="omi", occurred_at=occurred, consent=True, metadata=metadata)
    except ValidationError as exc:
        # A malformed client-supplied timestamp is a bad request, not a server
        # fault; surface it as 422 instead of an unhandled 500.
        raise HTTPException(status_code=422, detail="invalid Omi transcript field") from exc


def _omi_segment_events(payload: Any, uid: str, idempotency_key: str | None, hook: str) -> list[tuple[TranscriptEventIn, bool | None]]:
    if isinstance(payload, list):
        segments = payload
        envelope: dict[str, Any] = {}
    elif isinstance(payload, dict):
        segments = payload.get("segments") or payload.get("transcript_segments") or []
        envelope = payload
    else:
        raise HTTPException(status_code=422, detail="Omi transcript payload must be an object or array")
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
        events.append((TranscriptEventIn(event_id=event_id, device_id=uid, text=text, source="omi", consent=True, metadata=metadata), is_user))
    if not events:
        raise HTTPException(status_code=422, detail="Omi payload contains no non-empty transcript segments")
    return events


def create_app(config: Settings = settings) -> FastAPI:
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.db_path)
    archive_store = ArchiveStore(config.archive_db_path)
    vault = ArchiveVault(config.archive_key_file, config.audio_dir)
    authority = TokenAuthority(config.dashboard_token_file, config.device_tokens_dir, config.require_auth)
    crew = Crew(store, config)
    controls = ControlPlane(store)
    improvements = ImprovementManager(store, config.self_improvement_dir, controls)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if config.require_auth and not authority.ready():
            raise RuntimeError("Followthrough authentication secrets are not provisioned")
        store.migrate_plaintext_runs(vault.encrypt)
        migrated = archive_store.import_legacy(store.db)
        if migrated["events"] or migrated["audio_chunks"]:
            store.purge_split_archive_rows()
        store.scrub_legacy_operational_plaintext()
        yield

    application = FastAPI(title="Followthrough", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    application.state.settings = config
    application.state.store = store
    application.state.archive_store = archive_store
    application.state.vault = vault
    application.state.authority = authority
    application.state.crew = crew
    application.state.controls = controls
    application.state.improvements = improvements
    application.state.background_tasks = set()
    application.state.transcript_aggregators = {}
    static = Path(__file__).parent / "static"
    if static.is_dir():
        application.mount("/static", StaticFiles(directory=static), name="static")

    @application.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), payment=()"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def token(authorization: str | None, legacy: str | None) -> str:
        return bearer_token(authorization, legacy)

    def require_dashboard(authorization: str | None, legacy: str | None) -> None:
        if not authority.dashboard(token(authorization, legacy)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="dashboard authentication required")

    def require_device(authorization: str | None, legacy: str | None) -> str:
        candidate = token(authorization, legacy)
        if not authority.device(candidate):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="device authentication required")
        return hashlib.sha256(candidate.encode()).hexdigest()

    async def finish_run(run_id: str, event_id: str, transcript: str, classification: Any, allow_owner_report: bool) -> None:
        try:
            result = await asyncio.to_thread(crew.process, run_id, transcript, classification, allow_owner_report=allow_owner_report)
            await bus.publish("run_completed", {"event_id": event_id, **result})
        except Exception as exc:
            store.update_run(run_id, status="failed", finished_at=_utc(), success=0)
            store.add_eval(run_id, "[encrypted archive]", "completed autonomous run", f"worker failure: {type(exc).__name__}")
            await bus.publish("run_failed", {"event_id": event_id, "run_id": run_id, "error": type(exc).__name__})

    def authorize_capture(idempotency_key: str, actor: str) -> None:
        decision = controls.authorize(
            Capability.LISTENING,
            idempotency_key=idempotency_key,
            actor=actor,
        )
        if not decision.allowed:
            raise HTTPException(status_code=503, detail=decision.reason_code)

    async def enqueue_durable_job(run_id: str, archive_id: str, event_id: str, category: str, named_entity: str) -> dict[str, object]:
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
        }.get(category, "Research and evaluate the named item, then return cited findings and a safe next action.")
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
        )
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
        await bus.publish("job_queued", {"event_id": event_id, "run_id": run_id, "job_id": job["id"]})
        return {"status": "queued", "run_id": run_id, "job_id": job["id"], "orchestrator": "hermes-kanban"}

    async def ingest(
        payload: TranscriptEventIn,
        *,
        speaker: SpeakerContext,
        defer_actions: bool = False,
        suppress_dispatch: bool = False,
    ) -> dict[str, object]:
        transcript = payload.text.strip()
        if len(transcript.encode()) > config.max_transcript_bytes:
            raise HTTPException(status_code=413, detail="transcript exceeds configured limit")
        authorize_capture(f"transcript:{payload.event_id}", f"capture:{payload.source}")
        relevance = evaluate_relevance(transcript, speaker, store.interest_model())
        category = relevance.primary_category.value if relevance.primary_category else relevance.reason_code
        if relevance.dispatch_allowed:
            kind = category
        elif relevance.owner_status == OwnerStatus.NON_OWNER:
            kind = "non_owner_speaker"
        elif relevance.owner_status == OwnerStatus.UNKNOWN:
            kind = "owner_unverified"
        else:
            kind = relevance.reason_code
        classification = Classification(relevance.dispatch_allowed, kind, relevance.confidence, relevance.reason_code)
        associated_data = f"transcript:{payload.event_id}".encode()
        try:
            cipher = vault.encrypt(transcript.encode(), associated_data)
        except ArchiveIntegrityError as exc:
            raise HTTPException(status_code=503, detail="archive encryption unavailable") from exc
        archived, created = archive_store.archive_event(
            event_id=payload.event_id,
            device_id=payload.device_id,
            source=payload.source,
            occurred_at=_utc(payload.occurred_at),
            transcript_cipher=cipher,
            transcript_sha256=vault.digest(transcript.encode()),
            relevant=relevance.dispatch_allowed,
            classification=classification.kind,
            metadata={**payload.metadata, "relevance": relevance.to_dict()},
        )
        if not created:
            if archived["transcript_sha256"] != vault.digest(transcript.encode()):
                raise HTTPException(status_code=409, detail="event ID already exists with different transcript")
            if archived.get("run_id"):
                existing_run = store.get_run(archived["run_id"])
                return {"event_id": payload.event_id, "archive_id": archived["id"], "created": False, "status": existing_run["status"] if existing_run else "accepted", "run_id": archived["run_id"]}
            return {"event_id": payload.event_id, "archive_id": archived["id"], "created": False, "status": "archived", "classification": archived["classification"]}
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
        if not relevance.dispatch_allowed:
            await bus.publish("archive_only", {"event_id": payload.event_id, "classification": classification.kind})
            return {"event_id": payload.event_id, "archive_id": archived["id"], "created": True, "status": "archived", "classification": classification.__dict__, "relevance": relevance.to_dict(), "operational_memory": False}
        run_id = store.create_run(payload.source, classification.kind, "Followthrough signal", archived["id"])
        archive_store.link_run(archived["id"], run_id)
        subject = operational_entity(transcript, category)
        store.add_operational_memory(
            archived["id"], run_id, relevance.content_fingerprint, category, subject
        )
        allow_owner_report = bool(payload.metadata.get("allow_owner_report", True))
        if config.kanban_enabled:
            queued = await enqueue_durable_job(
                run_id, archived["id"], payload.event_id, category, subject
            )
            return {"event_id": payload.event_id, "archive_id": archived["id"], "created": True, "classification": classification.__dict__, **queued}
        if not controls.operation_allowed(Capability.ACTIONS):
            store.update_run(run_id, status="paused", success=None, finished_at=None)
            return {
                "event_id": payload.event_id,
                "archive_id": archived["id"],
                "created": True,
                "status": "parked",
                "run_id": run_id,
                "classification": classification.__dict__,
            }
        if defer_actions:
            task = asyncio.create_task(finish_run(run_id, payload.event_id, transcript, classification, allow_owner_report))
            application.state.background_tasks.add(task)
            task.add_done_callback(application.state.background_tasks.discard)
            return {"event_id": payload.event_id, "archive_id": archived["id"], "created": True, "status": "queued", "run_id": run_id, "classification": classification.__dict__}
        result = await asyncio.to_thread(crew.process, run_id, transcript, classification, allow_owner_report=allow_owner_report)
        await bus.publish("run_completed", {"event_id": payload.event_id, **result})
        return {"event_id": payload.event_id, "archive_id": archived["id"], "created": True, **result}

    @application.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (static / "index.html").read_text()

    @application.get("/healthz")
    async def healthz() -> dict[str, object]:
        database_ready = bool(store.db.execute("SELECT 1").fetchone()[0])
        archive_ready = vault.key_file.is_file() and archive_store.integrity_check()
        auth_ready = authority.ready()
        orchestrator = store.heartbeat_status("orchestrator") if config.kanban_enabled else None
        return {
            "ok": database_ready and archive_ready and auth_ready,
            "service": "followthrough",
            "version": "0.2.0",
            "database_ready": database_ready,
            "archive_ready": archive_ready,
            "auth_ready": auth_ready,
            "hermes_cli_present": shutil.which(config.hermes_bin) is not None,
            "orchestrator": orchestrator,
            "job_counts": store.hermes_job_counts(),
            "control_mode": controls.status()["global"]["mode"],
        }

    @application.post("/api/v1/transcripts", status_code=202)
    async def transcript_event(payload: TranscriptEventIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        principal = require_device(authorization, x_followthrough_token)
        # Bind every accepted event to the one-way identity of the device token.
        # This value is generated by the server and cannot be selected by the client.
        payload.metadata["capture_principal"] = principal
        if not payload.consent:
            raise HTTPException(status_code=400, detail="capture consent flag is required")
        speaker = SpeakerContext.native_owner(principal)
        aggregate = None
        aggregator = None
        already_archived = archive_store.by_event(payload.event_id) is not None
        if already_archived and payload.source in {"phone", "wearable"} and not payload.metadata.get("aggregated"):
            # Reconstruct only a self-contained one-segment aggregate on replay.
            # This returns the original aggregate's run/job receipt without
            # mixing newly buffered ambient context into a second aggregate.
            replay_aggregator = TranscriptAggregator()
            aggregate = replay_aggregator.add(
                Transcript(payload.event_id, _utc(payload.occurred_at), payload.text)
            )
            aggregator = replay_aggregator if aggregate else None
        elif (
            payload.source in {"phone", "wearable"}
            and not payload.metadata.get("aggregated")
        ):
            aggregator = application.state.transcript_aggregators.setdefault(
                payload.device_id, TranscriptAggregator()
            )
            aggregate = aggregator.add(
                Transcript(payload.event_id, _utc(payload.occurred_at), payload.text)
            )
        result = await ingest(
            payload,
            speaker=speaker,
            defer_actions=True,
            suppress_dispatch=aggregate is not None,
        )
        if aggregate and aggregator:
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
                    "aggregate_window_seconds": aggregator.window_seconds,
                },
            )
            aggregate_result = await ingest(
                aggregate_payload, speaker=speaker, defer_actions=True
            )
            return {
                **aggregate_result,
                "aggregate_event_id": aggregate.event_id,
                "original_event_id": payload.event_id,
            }
        return result

    def _authorize_event_principal(archived: dict[str, object], candidate: str) -> None:
        """Bind an audio operation to the archive event's capture principal.

        A dashboard token may inspect any event. A device token may only reach
        an event whose server-derived ``capture_principal`` matches its own
        one-way identity; a mismatch is reported as 404 so ownership of the
        opaque event id is never disclosed to another valid device.
        """

        if authority.dashboard(candidate):
            return
        principal = hashlib.sha256(candidate.encode()).hexdigest()
        try:
            metadata = json.loads(archived["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        bound = metadata.get("capture_principal")
        if bound != principal:
            raise HTTPException(status_code=404, detail="archive event not found")

    @application.get("/api/v1/audio/{event_id}/status")
    async def audio_status(
        event_id: str,
        authorization: str | None = Header(default=None),
        x_followthrough_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        candidate = token(authorization, x_followthrough_token)
        if not authority.dashboard_or_device(candidate):
            raise HTTPException(status_code=401, detail="authentication required")
        archived = archive_store.by_event(event_id)
        if not archived:
            raise HTTPException(status_code=404, detail="archive event not found")
        _authorize_event_principal(archived, candidate)
        return {"event_id": event_id, **archive_store.audio_manifest(archived["id"])}

    @application.put("/api/v1/audio/{event_id}/{sequence}")
    async def audio_chunk(
        event_id: str,
        sequence: int,
        request: Request,
        authorization: str | None = Header(default=None),
        x_followthrough_token: str | None = Header(default=None),
        x_device_id: str | None = Header(default=None),
        x_content_sha256: str | None = Header(default=None),
        content_type: str | None = Header(default="application/octet-stream"),
    ) -> dict[str, object]:
        require_device(authorization, x_followthrough_token)
        if sequence < 0:
            raise HTTPException(status_code=422, detail="sequence must be non-negative")
        if sequence > config.max_audio_sequence:
            raise HTTPException(status_code=422, detail="sequence exceeds configured maximum")
        authorize_capture(f"audio:{event_id}:{sequence}", "capture:native-audio")
        archived = archive_store.by_event(event_id)
        if not archived:
            raise HTTPException(status_code=404, detail="transcript event must be ingested before audio")
        _authorize_event_principal(archived, token(authorization, x_followthrough_token))
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
        digest = vault.digest(body)
        if x_content_sha256 and x_content_sha256.lower() != digest:
            raise HTTPException(status_code=422, detail="audio digest mismatch")
        mime = content_type or "application/octet-stream"
        associated_data = f"audio:{event_id}:{sequence}:{mime}:{digest}".encode()
        try:
            chunk, created = archive_store.persist_audio_chunk(
                archived["id"],
                sequence,
                mime,
                digest,
                len(body),
                lambda: vault.write_audio(archived["id"], sequence, body, associated_data),
            )
        except ArchiveIntegrityError as exc:
            raise HTTPException(status_code=503, detail="archive encryption unavailable") from exc
        if chunk["plaintext_sha256"] != digest:
            raise HTTPException(status_code=409, detail="audio sequence already exists with different content")
        return {"event_id": event_id, "sequence": sequence, "plaintext_sha256": digest, "plaintext_bytes": len(body), "created": created}

    @application.post("/api/signals")
    async def signal(payload: SignalIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        candidate = token(authorization, x_followthrough_token)
        if not authority.dashboard_or_device(candidate):
            raise HTTPException(status_code=401, detail="authentication required")
        if not payload.consent:
            raise HTTPException(status_code=400, detail="explicit consent is required")
        event = TranscriptEventIn(event_id="web:" + str(uuid.uuid4()), device_id="dashboard", text=payload.text, source=payload.source, consent=True, metadata={"email": str(payload.email) if payload.email else None, "allow_owner_report": payload.allow_owner_report})
        if payload.email:
            store.activate(str(payload.email))
        return await ingest(event, speaker=SpeakerContext.native_owner(hashlib.sha256(candidate.encode()).hexdigest()))

    @application.post("/api/webhooks/omi/transcript", status_code=202)
    async def omi_transcript(
        request: Request,
        secret: str = Query(alias="token"),
        uid: str = Query(min_length=1, max_length=200),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        if not authority.device(secret):
            raise HTTPException(status_code=401, detail="invalid Omi webhook secret")
        payload = await request.json()
        events = _omi_segment_events(payload, uid, idempotency_key, "transcript")
        receipts = [
            await ingest(
                event,
                speaker=SpeakerContext.omi(
                    is_user=is_user,
                    ambient_authorized=True,
                ),
                defer_actions=True,
            )
            for event, is_user in events
        ]
        return {"accepted": len(receipts), "receipts": receipts}

    @application.post("/api/webhooks/omi/conversation", status_code=202)
    async def omi_conversation(
        request: Request,
        secret: str = Query(alias="token"),
        uid: str = Query(min_length=1, max_length=200),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        if not authority.device(secret):
            raise HTTPException(status_code=401, detail="invalid Omi webhook secret")
        payload = await request.json()
        events = _omi_segment_events(payload, uid, idempotency_key, "conversation")
        receipts = [
            await ingest(
                event,
                speaker=SpeakerContext.omi(
                    is_user=is_user,
                    ambient_authorized=True,
                ),
                defer_actions=True,
            )
            for event, is_user in events
        ]
        return {"accepted": len(receipts), "receipts": receipts}

    @application.post("/api/webhooks/omi/audio", status_code=202)
    async def omi_audio(
        request: Request,
        secret: str | None = Query(default=None, alias="token"),
        uid: str = Query(min_length=1, max_length=200),
        sample_rate: int = Query(default=16_000, ge=8_000, le=96_000),
        timestamp: str | None = Query(default=None, max_length=100),
        sequence: str | None = Query(default=None, max_length=100),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        content_type: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_followthrough_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        device_secret = secret or token(authorization, x_followthrough_token)
        if not device_secret or not authority.device(device_secret):
            raise HTTPException(status_code=401, detail="invalid Omi webhook secret")
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > config.max_audio_chunk_bytes:
            raise HTTPException(status_code=413, detail="audio chunk exceeds configured limit")
        body = await request.body()
        if not body:
            raise HTTPException(status_code=422, detail="empty audio chunk")
        if len(body) > config.max_audio_chunk_bytes:
            raise HTTPException(status_code=413, detail="audio chunk exceeds configured limit")
        digest = vault.digest(body)
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
                device_identity=hashlib.sha256(device_secret.encode()).hexdigest(),
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
                raise HTTPException(status_code=409, detail="Omi audio idempotency key reused with different bytes")
            if existing_chunk:
                return {"event_id": event_id, "created": False, "plaintext_sha256": digest, "plaintext_bytes": len(body), "sample_rate": sample_rate}
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
                transcript_cipher=vault.encrypt(empty, f"transcript:{event_id}".encode()),
                transcript_sha256=vault.digest(empty),
                relevant=False,
                classification="audio_only",
                metadata={"sample_rate": sample_rate if pcm_delivery else None, "encoding": "pcm_s16le_mono" if pcm_delivery else mime, "idempotency_key": idempotency_key, "idempotency_source": idempotency_source, "omi_timestamp": timestamp, "omi_sequence": sequence, "capture_stream_id": stream_id, "stream_sequence": stream_sequence, "duration_ms": duration_ms, "alignment": "arrival_time_estimate"},
            )
        existing = archive_store.audio_chunk(archived["id"], 0)
        if existing:
            if existing["plaintext_sha256"] != digest:
                raise HTTPException(status_code=409, detail="Omi audio idempotency key reused with different bytes")
            return {"event_id": event_id, "created": False, "plaintext_sha256": digest, "plaintext_bytes": len(body)}
        mime = (content_type or "application/octet-stream").split(";", 1)[0].lower()
        if mime == "application/octet-stream":
            mime = "audio/L16"
        associated_data = f"audio:{event_id}:0:{mime}:{digest}".encode()
        chunk, created_chunk = archive_store.persist_audio_chunk(
            archived["id"],
            0,
            mime,
            digest,
            len(body),
            lambda: vault.write_audio(archived["id"], 0, body, associated_data),
        )
        if chunk["plaintext_sha256"] != digest:
            raise HTTPException(status_code=409, detail="Omi audio idempotency key reused with different bytes")
        return {"event_id": event_id, "created": created_chunk, "archive_created": created_event, "recovered_incomplete_event": not created_event and created_chunk, "plaintext_sha256": digest, "plaintext_bytes": len(body), "sample_rate": sample_rate}

    @application.post("/api/webhooks/omi")
    async def omi(request: Request, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None), x_device_id: str | None = Header(default=None)) -> dict[str, object]:
        require_device(authorization, x_followthrough_token)
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
            defer_actions=True,
        )

    @application.get("/api/relevance/{event_id}")
    async def relevance_decision(event_id: str, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        decision = store.relevance_for_event(event_id)
        if not decision:
            raise HTTPException(status_code=404, detail="relevance decision not found")
        return decision

    @application.post("/api/relevance/interests")
    async def set_interest(payload: InterestWeightIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        weight = InterestWeight(payload.category, payload.weight, payload.source)
        store.set_interest_weight(weight)
        return weight.to_dict()

    @application.post("/api/relevance/corrections", status_code=202)
    async def correct_relevance(payload: RelevanceCorrectionIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        archived = archive_store.by_event(payload.event_id)
        decision = store.relevance_for_event(payload.event_id)
        if not archived or not decision:
            raise HTTPException(status_code=404, detail="archive event or relevance decision not found")
        categories = tuple(payload.categories) or tuple(Category(value) for value in decision["categories"] if value != "ordinary_life")
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
            transcript = vault.decrypt(archived["transcript_cipher"], f"transcript:{payload.event_id}".encode()).decode()
        except (ArchiveIntegrityError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=503, detail="encrypted archive record is unavailable") from exc
        result = evaluate_relevance(transcript, speaker, store.interest_model())
        category = result.primary_category.value if result.primary_category else result.reason_code
        classification = Classification(result.dispatch_allowed, category if result.dispatch_allowed else result.reason_code, result.confidence, result.reason_code)
        archive_store.set_classification(archived["id"], relevant=result.dispatch_allowed, classification=classification.kind)
        store.record_relevance(archived["id"], payload.event_id, result.to_dict())
        run_id = archived.get("run_id")
        cancelled = False
        if result.dispatch_allowed and not run_id:
            run_id = store.create_run(archived["source"], classification.kind, "Corrected Followthrough signal", archived["id"])
            archive_store.link_run(archived["id"], run_id)
            subject = operational_entity(transcript, category)
            store.add_operational_memory(
                archived["id"], run_id, result.content_fingerprint, category, subject
            )
            if config.kanban_enabled:
                await enqueue_durable_job(
                    run_id, archived["id"], payload.event_id, category, subject
                )
            else:
                task = asyncio.create_task(finish_run(run_id, payload.event_id, transcript, classification, True))
                application.state.background_tasks.add(task)
                task.add_done_callback(application.state.background_tasks.discard)
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
    async def operational_memory(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> list[dict[str, object]]:
        require_dashboard(authorization, x_followthrough_token)
        return store.list_operational_memories()

    @application.get("/api/jobs")
    async def jobs(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> list[dict[str, object]]:
        require_dashboard(authorization, x_followthrough_token)
        return store.list_hermes_jobs()

    @application.get("/api/v1/jobs/{job_id}")
    async def device_job(
        job_id: str,
        authorization: str | None = Header(default=None),
        x_followthrough_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        principal = require_device(authorization, x_followthrough_token)
        job = store.hermes_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        archived = archive_store.by_id(job["archive_id"])
        metadata = json.loads(archived["metadata_json"]) if archived else {}
        if metadata.get("capture_principal") != principal:
            # Do not disclose whether another device owns the opaque identifier.
            raise HTTPException(status_code=404, detail="job not found")
        run = store.get_run(job["run_id"])
        summary = (run or {}).get("summary") or job.get("latest_outcome")
        return {
            "job_id": job["id"],
            "run_id": job["run_id"],
            "task_id": job.get("task_id"),
            "state": job["state"],
            "category": job.get("category"),
            "entity": job.get("entity"),
            "summary": summary,
            "error": job.get("last_error"),
            "updated_at": job["updated_at"],
        }

    @application.get("/api/controls")
    async def control_status(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        return controls.status()

    @application.get("/api/controls/audit")
    async def control_audit(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> list[dict[str, object]]:
        require_dashboard(authorization, x_followthrough_token)
        return controls.audit_log()

    @application.post("/api/controls/global")
    async def change_global_control(payload: GlobalControlIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        return controls.set_global_mode(
            payload.mode,
            actor=payload.actor,
            reason_code=payload.reason_code,
            resume_parked=payload.resume_parked,
        )

    @application.post("/api/controls/safe-mode")
    async def activate_safe_mode(payload: SafeModeIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        return controls.trigger_safe_mode(payload.trigger, actor=payload.actor)

    @application.post("/api/controls/capabilities/{capability}")
    async def change_capability(capability: str, payload: CapabilityControlIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
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
    async def change_capability_limit(capability: str, payload: CapabilityLimitIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
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
    async def park_job(run_id: str, payload: TaskControlIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        return controls.park_run(run_id, actor=payload.actor, reason_code=payload.reason_code)

    @application.post("/api/controls/jobs/resume")
    async def resume_jobs(payload: TaskControlIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        return controls.resume_parked(actor=payload.actor, reason_code=payload.reason_code)

    @application.get("/api/self-improvement")
    async def improvement_status(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        return {"policy": improvements.policy(), "proposals": improvements.list_proposals()}

    @application.post("/api/self-improvement/proposals", status_code=202)
    async def propose_improvement(payload: ImprovementProposalIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        try:
            return improvements.propose(
                target=payload.target,
                content=payload.content,
                evidence=[item.model_dump() for item in payload.evidence],
                created_by=payload.created_by,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/self-improvement/proposals/{proposal_id}/evaluate")
    async def evaluate_improvement(proposal_id: str, payload: ImprovementEvaluationIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        try:
            return improvements.evaluate(
                proposal_id,
                evaluator_id=payload.evaluator_id,
                held_in=(EvalCaseResult(**case.model_dump()) for case in payload.held_in),
                held_out=(EvalCaseResult(**case.model_dump()) for case in payload.held_out),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.put("/api/self-improvement/policy")
    async def configure_improvement_policy(payload: ImprovementPolicyIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        try:
            return improvements.configure_live_policy(
                live_enabled=payload.live_enabled,
                allowed_roots=payload.allowed_roots,
                required_approver_prefix=payload.required_approver_prefix,
                actor=payload.actor,
            )
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @application.post("/api/self-improvement/proposals/{proposal_id}/promote")
    async def promote_improvement(proposal_id: str, payload: ImprovementPromotionIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        try:
            return improvements.promote(
                proposal_id,
                approved_by=payload.approved_by,
                approval_reference=payload.approval_reference,
                live_root=payload.live_root,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @application.post("/api/signup")
    async def signup(payload: SignupIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, bool]:
        require_dashboard(authorization, x_followthrough_token)
        store.signup(str(payload.email), payload.source)
        return {"ok": True}

    @application.post("/api/roles")
    async def add_role(payload: RoleIn, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        return store.add_role(payload.name, payload.job, payload.tools, payload.guardrails)

    @application.get("/api/roles")
    async def roles(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> list[dict[str, object]]:
        require_dashboard(authorization, x_followthrough_token)
        return store.roles()

    @application.get("/api/runs")
    async def runs(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> list[dict[str, object]]:
        require_dashboard(authorization, x_followthrough_token)
        return store.list_runs()

    @application.get("/api/runs/{run_id}")
    async def run(run_id: str, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        found = store.get_run(run_id)
        if not found:
            raise HTTPException(status_code=404, detail="run not found")
        return found

    @application.get("/api/metrics")
    async def metrics(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
        require_dashboard(authorization, x_followthrough_token)
        return {**store.metrics(), **archive_store.metrics(), "job_counts": store.hermes_job_counts(), "orchestrator": store.heartbeat_status("orchestrator"), "integrations": {"hermes": shutil.which(config.hermes_bin) is not None, "convex": bool(config.convex_url), "linkup": bool(config.linkup_api_key), "elevenlabs": bool(config.elevenlabs_api_key), "dodo": bool(config.dodo_payments_api_key)}}

    @application.get("/api/activity")
    async def activity(
        authorization: str | None = Header(default=None),
        x_followthrough_token: str | None = Header(default=None),
    ) -> list[dict[str, object]]:
        require_dashboard(authorization, x_followthrough_token)
        result: list[dict[str, object]] = []
        for event in archive_store.recent_events(24):
            try:
                plaintext = vault.decrypt(
                    event["transcript_cipher"],
                    f"transcript:{event['event_id']}".encode(),
                ).decode("utf-8", errors="replace")
            except ArchiveIntegrityError:
                plaintext = "[encrypted transcript unavailable]"
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

    @application.get("/api/audio/{filename}")
    async def audio(filename: str, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> FileResponse:
        require_dashboard(authorization, x_followthrough_token)
        path = _safe_child(config.reports_dir.parent / "audio", filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="audio not found")
        return FileResponse(path, media_type="audio/mpeg")

    @application.get("/api/reports/{filename}")
    async def report(filename: str, authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> FileResponse:
        require_dashboard(authorization, x_followthrough_token)
        path = _safe_child(config.reports_dir, filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="report not found")
        return FileResponse(path, media_type="text/markdown")

    @application.get("/api/events")
    async def events(authorization: str | None = Header(default=None), x_followthrough_token: str | None = Header(default=None)) -> StreamingResponse:
        require_dashboard(authorization, x_followthrough_token)
        return StreamingResponse(bus.stream(), media_type="text/event-stream")

    return application


app = create_app()
