from datetime import UTC, datetime

from followthrough.adb_bridge import parse_whisper_line


def test_parse_omi_on_device_whisper_log() -> None:
    line = (
        "07-11 16:31:01.912 31873 31873 I flutter : \x1b[38;5;255m│ [debug] | "
        "16:31:01 911ms | [OnDeviceWhisper] Transcribed 11.4s in 10362ms "
        "(0.91x real-time). Text: Research github.com/example/tool\x1b[0m"
    )
    result = parse_whisper_line(line, day=datetime(2026, 7, 11, tzinfo=UTC))
    assert result is not None
    assert result.text == "Research github.com/example/tool"
    assert result.event_id.startswith("adb-omi:")
    assert result.occurred_at == "2026-07-11T16:31:01.912000+00:00"


def test_ignore_unrelated_log_line() -> None:
    assert parse_whisper_line("flutter: ordinary debug output") is None
