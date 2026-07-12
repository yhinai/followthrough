from pathlib import Path

from followthrough.archive import ArchiveStorage


def test_audio_file_is_stored_directly(tmp_path: Path) -> None:
    storage = ArchiveStorage(tmp_path / "audio")
    payload = b"RIFF-demo-audio-bytes"
    path = storage.write_audio("archive-123", 0, payload)
    assert path.read_bytes() == payload
    assert storage.read_audio(path) == payload
    assert path.stat().st_mode & 0o777 == 0o600
