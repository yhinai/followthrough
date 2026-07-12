from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
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
FINAL_COALESCE_SECONDS = 1.5

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

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
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
            "allow_owner_report": True,
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
        self.pending_finals: list[tuple[str, str | None]] = []
        self.final_flush_task: asyncio.Task[Any] | None = None

    def submit(self, event: UserInputTranscribedEvent) -> None:
        item_id = getattr(event, "item_id", None)
        task = asyncio.create_task(
            self._queue_final(event.transcript, item_id)
            if event.is_final
            else self._deliver_partial(event.transcript, item_id)
        )
        self.tasks.add(task)
        task.add_done_callback(self._task_done)

    async def close(self) -> None:
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        await self.client.aclose()

    async def _queue_final(self, text: str, item_id: str | None) -> None:
        clean = text.strip()
        if not clean:
            return
        async with self.lock:
            if item_id and item_id in self.finalized:
                return
            self.pending_finals.append((clean, item_id))
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
        if not pending:
            return
        text = " ".join(part for part, _ in pending)
        item_ids = [item_id for _, item_id in pending if item_id]
        utterance_id = (
            f"memo-livekit-{uuid.uuid5(uuid.NAMESPACE_URL, f'{self.ctx.room.name}:{':'.join(item_ids)}')}"
            if item_ids
            else self.utterance_id
        )
        result = await post_transcript(
            text=text,
            event_id=utterance_id,
            device_id=self.device_id,
            room_name=self.ctx.room.name,
            client=self.client,
        )
        self.finalized.update(item_ids)
        if len(self.finalized) > 256:
            self.finalized = set(tuple(self.finalized)[-256:])
        if not item_ids:
            self.utterance_id = self._new_utterance_id()
        self.sequences.pop(utterance_id, None)
        job_id = str(result.get("job_id") or "")
        if job_id and self._voice_enabled():
            task = asyncio.create_task(self._return_result(job_id))
            self.tasks.add(task)
            task.add_done_callback(self._task_done)

    async def _deliver_partial(self, text: str, item_id: str | None) -> None:
        clean = text.strip()
        if not clean:
            return
        async with self.lock:
            utterance_id = (
                f"memo-livekit-{uuid.uuid5(uuid.NAMESPACE_URL, f'{self.ctx.room.name}:{item_id}')}"
                if item_id
                else self.utterance_id
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

    async def _return_result(self, job_id: str) -> None:
        for _ in range(360):
            await asyncio.sleep(2)
            response = await self.client.get(
                f"{FOLLOWTHROUGH_API_URL}/api/v1/jobs/{job_id}"
            )
            if response.status_code != 200:
                continue
            result = response.json()
            state = str(result.get("state") or "")
            if state not in {"completed", "dead_letter", "needs_attention", "cancelled"}:
                continue
            summary = str(result.get("summary") or result.get("error") or state)
            await self.session.say(f"Followthrough result. {summary}")
            return

    def _device_id(self) -> str:
        for participant in self.ctx.room.remote_participants.values():
            try:
                metadata = json.loads(participant.metadata or "{}")
            except json.JSONDecodeError:
                continue
            device_id = metadata.get("device_id")
            if isinstance(device_id, str) and device_id.startswith("memo-"):
                return device_id[:100]
        return f"memo-livekit-{self.ctx.room.name[-20:]}"[:100]

    def _voice_enabled(self) -> bool:
        return _participant_response_mode(self.ctx) == "discord_and_voice"

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
        stt=inference.STT(model="deepgram/nova-3", language="multi"),
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
