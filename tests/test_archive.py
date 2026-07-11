from __future__ import annotations

from pathlib import Path

import pytest

from followthrough.archive import ArchiveIntegrityError, ArchiveVault


def test_aes_gcm_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    key = tmp_path / "archive.key"
    key.write_bytes(bytes(range(32)))
    vault = ArchiveVault(key, tmp_path / "audio")
    associated_data = b"transcript:event-123"
    encrypted = vault.encrypt(b"private conversation", associated_data)
    assert b"private conversation" not in encrypted
    assert vault.decrypt(encrypted, associated_data) == b"private conversation"
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(ArchiveIntegrityError):
        vault.decrypt(tampered, associated_data)


def test_audio_file_is_ciphertext(tmp_path: Path) -> None:
    key = tmp_path / "archive.key"
    key.write_bytes(bytes(range(32)))
    vault = ArchiveVault(key, tmp_path / "audio")
    payload = b"RIFF-demo-audio-bytes"
    associated_data = b"audio:event-123:0:audio/wav:digest"
    path = vault.write_audio("archive-123", 0, payload, associated_data)
    assert payload not in path.read_bytes()
    assert vault.read_audio(path, associated_data) == payload
