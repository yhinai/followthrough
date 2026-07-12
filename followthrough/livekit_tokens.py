from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from livekit import api


@dataclass(frozen=True)
class LiveKitSessionToken:
    server_url: str
    participant_token: str
    room_name: str
    participant_identity: str


def issue_memo_session_token(
    *,
    server_url: str,
    api_key: str,
    api_secret: str,
    agent_name: str,
    device_id: str,
    surface: str,
    response_mode: str,
    ttl_seconds: int,
) -> LiveKitSessionToken:
    if not server_url or not api_key or not api_secret:
        raise RuntimeError("LiveKit is not configured")

    suffix = hashlib.sha256(device_id.encode()).hexdigest()[:20]
    room_name = f"followthrough-{suffix}"
    identity = f"{device_id}-{suffix[:8]}"
    metadata = json.dumps(
        {
            "surface": surface,
            "device_id": device_id,
            "response_mode": response_mode,
            "capture_consent": True,
        },
        separators=(",", ":"),
    )
    room_config = api.RoomConfiguration(
        agents=[api.RoomAgentDispatch(agent_name=agent_name)]
    )
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name("Memo" if surface == "memo-android" else "Followthrough web")
        .with_metadata(metadata)
        .with_ttl(timedelta(seconds=ttl_seconds))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=False,
                can_publish_sources=["microphone"],
            )
        )
        .with_room_config(room_config)
        .to_jwt()
    )
    return LiveKitSessionToken(server_url, token, room_name, identity)
