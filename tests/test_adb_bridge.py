from datetime import UTC, datetime

from followthrough.adb_bridge import Transcript, TranscriptAggregator, parse_whisper_line


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


def segment(number: int, text: str) -> Transcript:
    return Transcript(f"event-{number}", f"2026-07-11T16:31:{number:02d}+00:00", text)


def test_aggregates_split_explicit_research_command() -> None:
    aggregator = TranscriptAggregator(window_seconds=45)
    assert aggregator.add(segment(1, "Followthrough, please research"), monotonic_at=1) is None
    result = aggregator.add(
        segment(2, "the GitHub repository pypa sampleproject"), monotonic_at=12
    )
    assert result is not None
    assert result.event_id.startswith("adb-omi:aggregate:")
    assert result.text == "Followthrough, please research the GitHub repository pypa sampleproject"


def test_aggregates_search_the_web_with_natural_connector_words() -> None:
    aggregator = TranscriptAggregator(window_seconds=45)
    result = aggregator.add(
        segment(1, "Oh, search the web and figure out how much caffeine is in a Red Bull."),
        monotonic_at=1,
    )
    assert result is not None
    assert result.text == "Oh, search the web and figure out how much caffeine is in a Red Bull."


def test_incomplete_commands_wait_for_the_next_segment() -> None:
    cases = [
        ("perform a web search", "for the latest World Cup news"),
        ("Followthrough, research the", "latest World Cup schedule"),
        ("check it out", "for current RTX 5090 prices"),
    ]
    for first_text, second_text in cases:
        aggregator = TranscriptAggregator(window_seconds=45)
        assert aggregator.add(segment(1, first_text), monotonic_at=1) is None
        assert aggregator.waiting_for_context is True
        result = aggregator.add(segment(2, second_text), monotonic_at=2)
        assert result is not None
        assert result.text == f"{first_text} {second_text}"
        assert aggregator.waiting_for_context is False


def test_bare_search_the_web_waits_instead_of_launching_without_a_query() -> None:
    aggregator = TranscriptAggregator(window_seconds=45)
    assert aggregator.add(segment(1, "search the web."), monotonic_at=1) is None
    assert aggregator.waiting_for_context is True


def test_does_not_aggregate_ordinary_or_passive_tool_conversation() -> None:
    aggregator = TranscriptAggregator(window_seconds=45)
    assert aggregator.add(segment(1, "The pizza was good"), monotonic_at=1) is None
    assert aggregator.add(segment(2, "They mentioned a useful tool"), monotonic_at=2) is None
    assert aggregator.add(segment(3, "and a startup founder"), monotonic_at=3) is None


def test_aggregate_excludes_ordinary_buffered_prefix() -> None:
    aggregator = TranscriptAggregator(window_seconds=45)
    assert aggregator.add(segment(1, "My medical appointment is tomorrow"), monotonic_at=1) is None
    assert aggregator.add(segment(2, "Followthrough please research"), monotonic_at=2) is None
    result = aggregator.add(segment(3, "the GitHub repository pypa sampleproject"), monotonic_at=3)
    assert result is not None
    assert result.text == "Followthrough please research the GitHub repository pypa sampleproject"
    assert "medical" not in result.text
    assert result.occurred_at == segment(2, "unused").occurred_at


def test_expired_action_segment_cannot_trigger() -> None:
    aggregator = TranscriptAggregator(window_seconds=10)
    assert aggregator.add(segment(1, "research this"), monotonic_at=1) is None
    assert aggregator.add(segment(2, "GitHub repository"), monotonic_at=20) is None


def test_aggregate_is_content_addressed_and_buffer_clears() -> None:
    first = TranscriptAggregator()
    second = TranscriptAggregator()
    items = [segment(1, "please test"), segment(2, "this github repo")]
    one = None
    two = None
    for index, item in enumerate(items):
        one = first.add(item, monotonic_at=index) or one
        two = second.add(item, monotonic_at=index) or two
    assert one is not None and two is not None
    assert one.event_id == two.event_id
    assert first.add(segment(3, "ordinary speech"), monotonic_at=3) is None


def test_local_logcat_clock_is_converted_to_utc() -> None:
    from datetime import timedelta, timezone

    plus_five = timezone(timedelta(hours=5))
    line = "09:00:00.000 | [OnDeviceWhisper] Transcribed 1.0s in 5ms. Text: check the repo"
    result = parse_whisper_line(line, day=datetime(2026, 7, 11, tzinfo=plus_five))
    assert result is not None
    # 09:00 at +05:00 is 04:00 UTC, not a relabeled 09:00 UTC.
    assert result.occurred_at == "2026-07-11T04:00:00+00:00"


def test_clock_without_fractional_seconds_does_not_crash() -> None:
    line = "09:00:00 | [OnDeviceWhisper] Transcribed 1s in 5ms. Text: schedule the meeting"
    result = parse_whisper_line(line, day=datetime(2026, 7, 11, tzinfo=UTC))
    assert result is not None
    assert result.occurred_at == "2026-07-11T09:00:00+00:00"
    assert result.text == "schedule the meeting"
