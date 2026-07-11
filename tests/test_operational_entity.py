from followthrough.integrations import operational_entity


def test_research_uses_only_named_url() -> None:
    text = "Please research https://github.com/pallets/itsdangerous after this private meeting"
    assert operational_entity(text, "repository") == "https://github.com/pallets/itsdangerous"


def test_todo_keeps_bounded_action_but_redacts_credential() -> None:
    text = "I need to send the build report and the API key is sk_abcdefghijklmnopqrstuvwxyz"
    value = operational_entity(text, "todo")
    assert value == "send the build report and the [redacted credential]"
    assert "sk_" not in value


def test_todo_stops_before_unrelated_conversation() -> None:
    text = (
        "We need to verify the recovery receipt tomorrow. "
        "Then somebody discussed a confidential unrelated meeting topic."
    )
    assert operational_entity(text, "todo") == "verify the recovery receipt tomorrow"


def test_todo_without_bounded_marker_never_copies_the_raw_segment() -> None:
    raw = "A long ambient segment mentions a task but has no explicit commitment marker"
    assert operational_entity(raw, "todo") == "Review and complete the captured commitment"


def test_non_actionable_generic_text_is_not_promoted() -> None:
    assert operational_entity("We discussed ordinary lunch plans", "ordinary_life") == (
        "the identified opportunity"
    )
