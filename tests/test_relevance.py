from __future__ import annotations

import json

import pytest

from followthrough.relevance import (
    CATEGORY_ORDER,
    Category,
    CorrectionRecord,
    Disposition,
    InterestModel,
    InterestWeight,
    OwnerStatus,
    Provenance,
    SpeakerContext,
    content_fingerprint,
    evaluate_relevance,
)


OMI_OWNER = SpeakerContext.omi(is_user=True)
NATIVE_OWNER = SpeakerContext.native_owner("sha256:owner-device")


def test_unknown_speaker_is_held_even_for_specific_repository() -> None:
    raw = "Clone and run https://github.com/BasedHardware/omi"
    result = evaluate_relevance(raw)

    assert result.actionable is False
    assert result.disposition == Disposition.HOLD
    assert result.owner_status == OwnerStatus.UNKNOWN
    assert result.primary_category == Category.REPOSITORY
    assert result.hermes_payload(raw) is None
    assert raw not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    ("is_user", "expected_status", "expected_actionable"),
    [
        (True, OwnerStatus.OWNER, True),
        (False, OwnerStatus.NON_OWNER, False),
        (None, OwnerStatus.UNKNOWN, False),
    ],
)
def test_omi_owner_requires_explicit_boolean_true(
    is_user: bool | None,
    expected_status: OwnerStatus,
    expected_actionable: bool,
) -> None:
    result = evaluate_relevance("Benchmark this new inference tool", SpeakerContext.omi(is_user=is_user))
    assert result.owner_status == expected_status
    assert result.actionable is expected_actionable


@pytest.mark.parametrize("is_user", [False, None])
def test_authenticated_omi_ambient_can_dispatch_without_owner_attribution(
    is_user: bool | None,
) -> None:
    context = SpeakerContext.omi(is_user=is_user, ambient_authorized=True)
    result = evaluate_relevance("Benchmark this new inference tool", context)

    expected_status = OwnerStatus.NON_OWNER if is_user is False else OwnerStatus.UNKNOWN
    assert result.owner_status == expected_status
    assert result.ambient_authorized is True
    assert result.actionable is True
    assert result.dispatch_allowed is True
    assert result.disposition == Disposition.ACTION
    assert result.reason_code == "ambient_relevant_signal"


@pytest.mark.parametrize("is_user", [False, None])
def test_authenticated_omi_ambient_still_ignores_irrelevant_speech(
    is_user: bool | None,
) -> None:
    result = evaluate_relevance(
        "Lunch was delicious and the coffee was great.",
        SpeakerContext.omi(is_user=is_user, ambient_authorized=True),
    )

    assert result.ambient_authorized is True
    assert result.actionable is False
    assert result.dispatch_allowed is False
    assert result.disposition == Disposition.IGNORE
    assert result.primary_category == Category.ORDINARY_LIFE


def test_generic_to_do_phrase_is_not_an_explicit_task() -> None:
    result = evaluate_relevance(
        "What do you want to do after lunch?",
        OMI_OWNER,
    )
    assert result.dispatch_allowed is False
    assert Category.TODO not in result.categories


def test_untrusted_is_user_claim_does_not_bypass_provenance() -> None:
    context = SpeakerContext(provenance=Provenance.UNKNOWN, is_user=True)
    result = evaluate_relevance("Research this SDK", context)
    assert result.owner_status == OwnerStatus.UNKNOWN
    assert result.actionable is False


@pytest.mark.parametrize(
    "context",
    [
        SpeakerContext(provenance=Provenance.UNKNOWN, ambient_authorized=True),
        SpeakerContext(
            provenance=Provenance.NATIVE_AUTHENTICATED,
            authenticated_owner=False,
            principal_fingerprint="sha256:guest",
            ambient_authorized=True,
        ),
    ],
)
def test_ambient_claim_does_not_bypass_non_omi_speaker_gate(
    context: SpeakerContext,
) -> None:
    result = evaluate_relevance("Research this SDK", context)

    assert result.ambient_authorized is False
    assert result.actionable is False
    assert result.dispatch_allowed is False
    assert result.disposition == Disposition.HOLD


def test_native_authenticated_owner_can_dispatch() -> None:
    raw = "Research this MCP server and benchmark its latency"
    result = evaluate_relevance(raw, NATIVE_OWNER)
    assert result.actionable is True
    assert result.dispatch_allowed is True
    assert result.hermes_payload(raw) == {
        "content_fingerprint": result.content_fingerprint,
        "category": "tool",
        "categories": ["tool", "performance"],
        "text": raw,
    }


def test_native_authenticated_non_owner_is_held() -> None:
    result = evaluate_relevance(
        "Add this GitHub repository to the task list",
        SpeakerContext.native_non_owner("sha256:guest"),
    )
    assert result.owner_status == OwnerStatus.NON_OWNER
    assert result.disposition == Disposition.HOLD
    assert result.actionable is False


def test_categories_are_deterministically_layered() -> None:
    result = evaluate_relevance(
        "Clone the GitHub repo for this SDK startup, benchmark latency, add it as a todo, "
        "register for its hackathon, email Maya, and make shipping it our goal.",
        OMI_OWNER,
    )
    assert result.categories == CATEGORY_ORDER
    assert result.primary_category == Category.REPOSITORY


@pytest.mark.parametrize(
    "text",
    [
        "Lunch was delicious and the coffee was great.",
        "Traffic was slow and the weather is nice today.",
        "I am tired, so I will take a nap.",
        "The dog is cute. Good night.",
    ],
)
def test_ordinary_life_is_ignored(text: str) -> None:
    result = evaluate_relevance(text, OMI_OWNER)
    assert result.actionable is False
    assert result.disposition == Disposition.IGNORE
    assert result.primary_category == Category.ORDINARY_LIFE


def test_action_signal_wins_over_ordinary_word() -> None:
    result = evaluate_relevance("Research this coffee startup", OMI_OWNER)
    assert result.actionable is True
    assert result.primary_category == Category.STARTUP


def test_interest_and_correction_models_round_trip_as_json() -> None:
    fingerprint = content_fingerprint("Research this unusual subject")
    model = (
        InterestModel()
        .with_weight(InterestWeight(Category.TOOL, 0.7, "learned"))
        .with_correction(
            CorrectionRecord(
                content_fingerprint=fingerprint,
                disposition=Disposition.ACTION,
                categories=(Category.TOOL,),
                reason_code="owner_marked_relevant",
            )
        )
    )
    encoded = json.dumps(model.to_dict(), sort_keys=True)
    decoded = InterestModel.from_dict(json.loads(encoded))
    assert decoded == model
    assert "Research this unusual subject" not in encoded


def test_correction_cannot_bypass_owner_gate() -> None:
    raw = "This ambiguous thing matters"
    model = InterestModel().with_correction(
        CorrectionRecord(
            content_fingerprint=content_fingerprint(raw),
            disposition=Disposition.ACTION,
            categories=(Category.GOAL,),
        )
    )
    result = evaluate_relevance(raw, SpeakerContext.unknown(), model)
    assert result.disposition == Disposition.HOLD
    assert result.actionable is False
    assert result.hermes_payload(raw) is None


def test_explicit_interest_mute_is_respected() -> None:
    model = InterestModel().with_weight(InterestWeight(Category.STARTUP, -1.0))
    result = evaluate_relevance("Research this startup", OMI_OWNER, model)
    assert result.disposition == Disposition.IGNORE
    assert result.reason_code == "interest_muted"


# This corpus is intentionally separate from the rule-specific tests above.  It
# exercises owner provenance, all eight categories, and ordinary-life negatives.
HELD_OUT_CASES: tuple[tuple[str, SpeakerContext, bool], ...] = (
    ("Please inspect https://github.com/astral-sh/uv", OMI_OWNER, True),
    ("Clone that repository and run its tests", NATIVE_OWNER, True),
    ("Evaluate the new vector database SDK", OMI_OWNER, True),
    ("This MCP server might help Hermes", NATIVE_OWNER, True),
    ("Look into the startup founded by Amira", OMI_OWNER, True),
    ("Their seed round makes this SaaS worth researching", NATIVE_OWNER, True),
    ("Profile the inference bottleneck and reduce latency", OMI_OWNER, True),
    ("Benchmark GPU utilization before deploying", NATIVE_OWNER, True),
    ("Remind me to review the proposal", OMI_OWNER, True),
    ("We need to update the deployment", NATIVE_OWNER, True),
    ("Register for the agent workshop", OMI_OWNER, True),
    ("Add the conference to my calendar", NATIVE_OWNER, True),
    ("Follow up with Maya tomorrow", OMI_OWNER, True),
    ("Email the founder about the demo", NATIVE_OWNER, True),
    ("Our objective is to ship the assistant", OMI_OWNER, True),
    ("I want to build a private knowledge system", NATIVE_OWNER, True),
    ("The pizza was excellent", OMI_OWNER, False),
    ("Should we get coffee after lunch?", NATIVE_OWNER, False),
    ("Traffic is bad because of the weather", OMI_OWNER, False),
    ("Good morning, the dog is cute", NATIVE_OWNER, False),
    ("I am hungry and need to eat dinner", OMI_OWNER, False),
    ("Let's watch a movie tonight", NATIVE_OWNER, False),
    ("I saw my email and then he replied", NATIVE_OWNER, False),
    ("The email address was on the screen", NATIVE_OWNER, False),
    ("That GitHub repository looks useful", SpeakerContext.unknown(), False),
    ("Try this new CLI tool", SpeakerContext.omi(is_user=None), False),
    ("Optimize the agent throughput", SpeakerContext.omi(is_user=False), False),
    ("Remind me to deploy it", SpeakerContext.native_non_owner("guest"), False),
    ("The room is quiet", OMI_OWNER, False),
    ("", OMI_OWNER, False),
)


def test_held_out_actionable_precision_and_recall_are_at_least_ninety_percent() -> None:
    predictions = [evaluate_relevance(text, speaker).actionable for text, speaker, _ in HELD_OUT_CASES]
    labels = [expected for _, _, expected in HELD_OUT_CASES]

    true_positive = sum(predicted and actual for predicted, actual in zip(predictions, labels))
    false_positive = sum(predicted and not actual for predicted, actual in zip(predictions, labels))
    false_negative = sum(not predicted and actual for predicted, actual in zip(predictions, labels))
    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / (true_positive + false_negative)

    assert len(HELD_OUT_CASES) >= 20
    assert precision >= 0.90
    assert recall >= 0.90
