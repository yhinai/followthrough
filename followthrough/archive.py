from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path


class ArchiveStorage:
    """Simple local byte storage for transcripts and audio."""

    def __init__(self, audio_dir: Path) -> None:
        self.audio_dir = audio_dir
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def audio_path(self, archive_id: str, sequence: int) -> Path:
        return self.audio_dir / archive_id / f"{sequence:08d}.audio"

    def write_audio(self, archive_id: str, sequence: int, payload: bytes) -> Path:
        path = self.audio_path(archive_id, sequence)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{secrets.token_hex(8)}.tmp")
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return path

    @staticmethod
    def read_audio(path: Path) -> bytes:
        return path.read_bytes()
