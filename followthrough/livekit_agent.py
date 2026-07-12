from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    CloseEvent,
    JobContext,
    RunContext,
    UserInputTranscribedEvent,
    cli,
    function_tool,
    inference,
    room_io,
)
from livekit.agents.llm import ChatContext, ChatMessage, StopResponse

logger = logging.getLogger("followthrough.livekit")
FINAL_DELIVERY_ATTEMPTS = 5
# Deepgram can emit adjacent final segments almost two seconds apart even when
# the speaker never pauses (for example, "Memo, can you search" followed by
# "the web and find ...").  Keep one short trailing window so those finals
# become one archive event and one action instead of dispatching a fragment.
FINAL_COALESCE_SECONDS = 3.0
DEEPGRAM_ENDPOINTING_MS = 1200

FOLLOWTHROUGH_API_URL = os.getenv(
    "FOLLOWTHROUGH_LIVEKIT_API_URL", "https://followthrough.alhinai.dev"
).rstrip("/")

MEMO_ACTIVATION = re.compile(r"\b(?:hey\s+)?memo\s*[,;:\-]?\s+", re.IGNORECASE)
INTERACTIVE_ACTIVATION = re.compile(r"^\s*(?:hey\s+)?followthrough\b", re.IGNORECASE)


async def forward_confirmed_signal(signal: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{FOLLOWTHROUGH_API_URL}/api/signals",
            json={"text": signal.strip(), "source": "voice", "consent": True},
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}


class FollowthroughVoiceAgent(Agent):
    def __init__(self, *, voice_enabled: bool) -> None:
        self.interactive_active = False
        self.voice_enabled = voice_enabled
        super().__init__(
            llm=inference.LLM(model="google/gemma-4-31b-it"),
            instructions=(
                "You are Followthrough's optional interactive voice surface. Keep replies brief. "
                "When directly addressed as Followthrough, clarify the request, repeat the exact "
                "signal, and ask for confirmation. Call submit_signal only after an unambiguous "
                "yes. Never react to ambient speech. Memo commands are handled automatically by "
                "Spark and do not need confirmation."
            ),
        )

    async def on_user_turn_completed(self, turn_ctx: ChatContext, new_message: ChatMessage) -> None:
        del turn_ctx
        text = new_message.text_content or ""
        if MEMO_ACTIVATION.search(text):
            if self.voice_enabled:
                self.session.say(
                    "Got it. I’ll handle that in the background.", add_to_chat_ctx=False
                )
            raise StopResponse()
        if INTERACTIVE_ACTIVATION.search(text):
            self.interactive_active = True
            return
        if not self.interactive_active:
            raise StopResponse()

    @function_tool
    async def submit_signal(self, context: RunContext, signal: str) -> str:
        """Submit the clarified signal only after explicit confirmation."""

        del context
        self.interactive_active = False
        result = await forward_confirmed_signal(signal)
        return (
            "Confirmed. I handed it to Followthrough."
            if result.get("job_id") or result.get("run_id")
            else "Confirmed. It was archived without creating work."
        )


async def post_transcript(
    *,
    text: str,
    event_id: str,
    device_id: str,
    room_name: str,
    speaker_mode: str = "personal",
    speaker_id: str | None = None,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    payload = {
        "event_id": event_id,
        "device_id": device_id,
        "text": text.strip(),
        "source": "phone",
        "consent": True,
        "metadata": {
            "capture": "memo_livekit",
            "utterance_id": event_id,
            "livekit_room": room_name,
            "speaker_mode": speaker_mode,
            "speaker_id": speaker_id,
            # A dedicated personal capture device is an owner-verifying
            # surface. Meeting audio is diarized but deliberately unverified;
            # the policy layer must not silently act for another attendee.
            "allow_owner_report": speaker_mode == "personal",
        },
    }
    for attempt in range(1, FINAL_DELIVERY_ATTEMPTS + 1):
        try:
            response = await client.post(
                f"{FOLLOWTHROUGH_API_URL}/api/v1/transcripts", json=payload
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Followthrough returned an invalid transcript response")
            return result
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else 0
            retryable = not status or status in {408, 429} or status >= 500
            if not retryable or attempt == FINAL_DELIVERY_ATTEMPTS:
                raise
            logger.warning(
                "Final transcript delivery failed; retrying",
                extra={"event_id": event_id, "attempt": attempt, "status": status},
            )
            await asyncio.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError("final transcript delivery exhausted")  # pragma: no cover


def _participant_response_mode(ctx: JobContext) -> str:
    for participant in ctx.room.remote_participants.values():
        try:
            metadata = json.loads(participant.metadata or "{}")
        except json.JSONDecodeError:
            continue
        mode = metadata.get("response_mode")
        if isinstance(mode, str):
            return mode
    return "discord_only"


class TranscriptBridge:
    def __init__(self, *, ctx: JobContext, session: AgentSession) -> None:
        self.ctx = ctx
        self.session = session
        self.client = httpx.AsyncClient(timeout=20)
        self.device_id = self._device_id()
        self.utterance_id = self._new_utterance_id()
        self.sequences: dict[str, int] = {}
        self.finalized: set[str] = set()
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.pending_finals: list[tuple[str, str | None, str | None]] = []
        self.final_flush_task: asyncio.Task[Any] | None = None
        self.last_transcript_activity_at: str | None = None
        self.spoken_delivery_receipts: set[str] = set()
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def submit(self, event: UserInputTranscribedEvent) -> None:
        self.last_transcript_activity_at = datetime.now(UTC).isoformat()
        item_id = getattr(event, "item_id", None)
        speaker_id = getattr(event, "speaker_id", None)
        task = asyncio.create_task(
            self._queue_final(event.transcript, item_id, speaker_id)
            if event.is_final
            else self._deliver_partial(event.transcript, item_id, speaker_id)
        )
        self.tasks.add(task)
        task.add_done_callback(self._task_done)

    async def close(self) -> None:
        self.heartbeat_task.cancel()
        await asyncio.gather(self.heartbeat_task, return_exceptions=True)
        await self._send_presence(state="offline", microphone_published=False)
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        await self.client.aclose()

    async def _heartbeat_loop(self) -> None:
        while True:
            await self._send_presence(state="listening", microphone_published=True)
            if self._voice_enabled():
                await self._drain_phone_deliveries()
            await asyncio.sleep(5)

    async def _drain_phone_deliveries(self) -> None:
        """Speak durable results and ACK only after audio was accepted by LiveKit."""

        try:
            response = await self.client.get(
                f"{FOLLOWTHROUGH_API_URL}/api/v1/devices/{self.device_id}/deliveries",
                params={"limit": 10},
            )
            response.raise_for_status()
            deliveries = response.json()
            if not isinstance(deliveries, list):
                raise ValueError("Followthrough returned invalid phone deliveries")
            for delivery in deliveries:
                if not isinstance(delivery, dict):
                    continue
                receipt_id = str(delivery.get("receipt_id") or "")
                if not receipt_id:
                    continue
                if receipt_id not in self.spoken_delivery_receipts:
                    summary = str(
                        delivery.get("summary")
                        or delivery.get("state")
                        or "Your Followthrough task finished."
                    )
                    await self.session.say(f"Followthrough result. {summary}")
                    self.spoken_delivery_receipts.add(receipt_id)
                ack = await self.client.post(
                    f"{FOLLOWTHROUGH_API_URL}/api/v1/devices/{self.device_id}/deliveries/ack",
                    json={"receipt_id": receipt_id},
                )
                ack.raise_for_status()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(
                "LiveKit phone delivery drain failed",
                extra={"device_id": self.device_id, "error": str(error)},
            )

    async def _send_presence(self, *, state: str, microphone_published: bool) -> None:
        metadata = self._participant_metadata()
        try:
            response = await self.client.post(
                f"{FOLLOWTHROUGH_API_URL}/api/v1/devices/heartbeat",
                json={
                    "device_id": self.device_id,
                    "room_name": self.ctx.room.name,
                    "surface": metadata.get("surface", "memo-android"),
                    "response_mode": metadata.get("response_mode", "discord_only"),
                    "state": state,
                    "microphone_published": microphone_published,
                    "last_transcript_activity_at": self.last_transcript_activity_at,
                },
            )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(
                "LiveKit device heartbeat failed",
                extra={"room": self.ctx.room.name, "error": str(error)},
            )

    async def _queue_final(
        self, text: str, item_id: str | None, speaker_id: str | None = None
    ) -> None:
        clean = text.strip()
        if not clean:
            return
        async with self.lock:
            pending_item_ids = {pending_id for _, pending_id, _ in self.pending_finals}
            if item_id and (item_id in self.finalized or item_id in pending_item_ids):
                return
            # A coalescing window may span two attendees in a meeting. Flush
            # the previous speaker immediately rather than manufacturing one
            # utterance attributed to both people.
            pending_speaker = self.pending_finals[0][2] if self.pending_finals else speaker_id
            if self.pending_finals and pending_speaker != speaker_id:
                pending = self.pending_finals
                self.pending_finals = []
                task = asyncio.create_task(self._deliver_finals(pending))
                self.tasks.add(task)
                task.add_done_callback(self._task_done)
            self.pending_finals.append((clean, item_id, speaker_id))
            if self.final_flush_task and not self.final_flush_task.done():
                self.final_flush_task.cancel()
            self.final_flush_task = asyncio.create_task(self._flush_finals())
            self.tasks.add(self.final_flush_task)
            self.final_flush_task.add_done_callback(self._task_done)

    async def _flush_finals(self) -> None:
        await asyncio.sleep(FINAL_COALESCE_SECONDS)
        async with self.lock:
            pending = self.pending_finals
            self.pending_finals = []
            self.final_flush_task = None
        await self._deliver_finals(pending)

    async def _deliver_finals(
        self, pending: list[tuple[str, str | None, str | None]]
    ) -> None:
        if not pending:
            return
        text = " ".join(part for part, _, _ in pending)
        item_ids = [item_id for _, item_id, _ in pending if item_id]
        speaker_id = pending[0][2]
        utterance_id = (
            f"memo-livekit-{uuid.uuid5(uuid.NAMESPACE_URL, f'{self.ctx.room.name}:{":".join(item_ids)}')}"
            if item_ids
            else self.utterance_id
        )
        await post_transcript(
            text=text,
            event_id=utterance_id,
            device_id=self.device_id,
            room_name=self.ctx.room.name,
            speaker_mode=self._speaker_mode(),
            speaker_id=speaker_id,
            client=self.client,
        )
        self.finalized.update(item_ids)
        if len(self.finalized) > 256:
            self.finalized = set(tuple(self.finalized)[-256:])
        if not item_ids:
            self.utterance_id = self._new_utterance_id()
        self.sequences.pop(utterance_id, None)

    async def _deliver_partial(
        self, text: str, item_id: str | None, speaker_id: str | None = None
    ) -> None:
        clean = text.strip()
        if not clean:
            return
        async with self.lock:
            utterance_id = (
                f"memo-livekit-{uuid.uuid5(uuid.NAMESPACE_URL, f'{self.ctx.room.name}:{item_id}')}"
                if item_id
                else (
                    f"{self.utterance_id}-{speaker_id}"
                    if speaker_id
                    else self.utterance_id
                )
            )
            sequence = self.sequences.get(utterance_id, 0) + 1
            self.sequences[utterance_id] = sequence
            response = await self.client.post(
                f"{FOLLOWTHROUGH_API_URL}/api/v1/transcripts/partial",
                json={
                    "utterance_id": utterance_id,
                    "device_id": self.device_id,
                    "text": clean,
                    "source": "phone",
                    "seq": sequence,
                    "consent": True,
                },
            )
            response.raise_for_status()

    def _device_id(self) -> str:
        device_id = self._participant_metadata().get("device_id")
        if isinstance(device_id, str) and re.fullmatch(r"[a-z0-9-]{3,100}", device_id):
            return device_id[:100]
        return f"memo-livekit-{self.ctx.room.name[-20:]}"[:100]

    def _participant_metadata(self) -> dict[str, Any]:
        for participant in self.ctx.room.remote_participants.values():
            try:
                metadata = json.loads(participant.metadata or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(metadata, dict) and metadata.get("device_id"):
                return metadata
        return {}

    def _voice_enabled(self) -> bool:
        return _participant_response_mode(self.ctx) == "discord_and_voice"

    def _speaker_mode(self) -> str:
        mode = self._participant_metadata().get("speaker_mode")
        return mode if mode in {"personal", "meeting"} else "personal"

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self.tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "LiveKit transcript bridge task failed",
                exc_info=(type(error), error, error.__traceback__),
                extra={"room": self.ctx.room.name},
            )

    @staticmethod
    def _new_utterance_id() -> str:
        return f"memo-livekit-{uuid.uuid4()}"


server = AgentServer(num_idle_processes=1)


@server.rtc_session(agent_name="followthrough")
async def followthrough_session(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name, "surface": "memo-livekit"}
    await ctx.connect()
    session = AgentSession(
        stt=inference.STT(
            model="deepgram/nova-3",
            language="multi",
            # Deepgram's low default endpoint threshold can split a continuous
            # sentence at tiny natural hesitations. Require a meaningful pause
            # before emitting a final; the bridge then coalesces any remaining
            # adjacent finals into a single archived utterance.
            extra_kwargs={"endpointing": DEEPGRAM_ENDPOINTING_MS, "diarize": True},
        ),
        tts=inference.TTS(
            model="cartesia/sonic-3",
            voice="9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
        ),
    )
    bridge = TranscriptBridge(ctx=ctx, session=session)

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event: UserInputTranscribedEvent) -> None:
        bridge.submit(event)

    @session.on("close")
    def on_close(event: CloseEvent) -> None:
        del event
        asyncio.create_task(bridge.close())

    await session.start(
        agent=FollowthroughVoiceAgent(
            voice_enabled=_participant_response_mode(ctx) == "discord_and_voice"
        ),
        room=ctx.room,
        room_options=room_io.RoomOptions(audio_input=True, audio_output=True),
        record=False,
    )


if __name__ == "__main__":
    cli.run_app(server)
