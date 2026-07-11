from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"FTA1"


class ArchiveIntegrityError(ValueError):
    pass


class ArchiveVault:
    def __init__(self, key_file: Path, audio_dir: Path) -> None:
        self.key_file = key_file
        self.audio_dir = audio_dir
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    def _key(self) -> bytes:
        try:
            key = self.key_file.read_bytes()
        except OSError as exc:
            raise ArchiveIntegrityError(f"archive key unavailable: {self.key_file}") from exc
        if len(key) != 32:
            raise ArchiveIntegrityError("archive key must be exactly 32 bytes")
        return key

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return MAGIC + nonce + AESGCM(self._key()).encrypt(nonce, plaintext, associated_data)

    def decrypt(self, envelope: bytes, associated_data: bytes) -> bytes:
        if len(envelope) < len(MAGIC) + 12 + 16 or not envelope.startswith(MAGIC):
            raise ArchiveIntegrityError("invalid archive envelope")
        nonce = envelope[len(MAGIC) : len(MAGIC) + 12]
        ciphertext = envelope[len(MAGIC) + 12 :]
        try:
            return AESGCM(self._key()).decrypt(nonce, ciphertext, associated_data)
        except InvalidTag as exc:
            raise ArchiveIntegrityError("archive authentication failed") from exc

    @staticmethod
    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def audio_path(self, archive_id: str, sequence: int) -> Path:
        return self.audio_dir / archive_id / f"{sequence:08d}.fta"

    def write_audio(self, archive_id: str, sequence: int, payload: bytes, associated_data: bytes) -> Path:
        path = self.audio_path(archive_id, sequence)
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self.encrypt(payload, associated_data)
        temporary = path.with_name(path.name + f".{secrets.token_hex(8)}.tmp")
        temporary.write_bytes(encrypted)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return path

    def read_audio(self, path: Path, associated_data: bytes) -> bytes:
        return self.decrypt(path.read_bytes(), associated_data)
