import pytest

from followthrough.relevance import (
    Category,
    CorrectionRecord,
    Disposition,
    InterestModel,
    InterestWeight,
    SpeakerContext,
    content_fingerprint,
    evaluate_relevance,
)


ACTIONABLE = [
    "What's the current price of gold today?",
    "Check RTX 5080 stock on Best Buy",
    "Find the price of a MacBook at Apple",
    "How much is bitcoin today?",
    "Research https://github.com/pypa/sampleproject",
    "Clone the GitHub repository for this SDK",
    "Look into the LangGraph framework",
    "Benchmark this inference library",
    "This agent API could make Hermes faster",
    "Remind me to call Maya tomorrow",
    "I need to prepare the demo tonight",
    "Add the investor meeting to my calendar",
    "Schedule a meeting with Sam on Tuesday",
    "Follow up with the founder of Acme",
    "Research this startup before we invest",
    "The goal is to automate customer support",
    "We should research that new tool",
    "Search online for the latest H Company docs",
    "Book the cheapest flight to Tokyo",
    "Sign me up for the AI event",
]

ORDINARY = [
    "Lunch was great and the sandwich was perfect",
    "The weather is nice today",
    "Traffic was terrible this morning",
    "I would like another coffee",
    "That pizza was delicious",
    "How are you doing?",
    "This room is cold",
    "I slept well last night",
    "We walked around the park",
    "The music is too loud",
    "I like this chair",
    "See you later",
    "That was funny",
    "Please pass the water",
    "The train was crowded",
    "My shoes are wet",
    "It is a beautiful day",
    "We had dinner at home",
    "The movie was long",
    "I am going downstairs",
]


def test_memo_activation_always_promotes_owner_command() -> None:
    result = evaluate_relevance(
        "Memo, summarize what I just asked and handle it.",
        SpeakerContext.native_owner("memo-phone"),
    )

    assert result.dispatch_allowed is True
    assert result.primary_category == Category.WEB_TASK
    assert result.reason_code == "owner_explicit_memo_command"
    assert any(item.rule_id == "activation.memo_explicit" for item in result.evidence)


def test_memo_activation_overrides_muted_category() -> None:
    interests = InterestModel(weights=(InterestWeight(Category.WEB_TASK, -1.0),))
    result = evaluate_relevance(
        "Memo, check the price of gold today.",
        SpeakerContext.native_owner("memo-phone"),
        interests,
    )

    assert result.dispatch_allowed is True
    assert result.reason_code == "owner_explicit_memo_command"


def test_memo_activation_overrides_prior_ignore_correction() -> None:
    text = "Memo, check the price of gold today."
    interests = InterestModel(
        corrections=(
            CorrectionRecord(
                content_fingerprint=content_fingerprint(text),
                disposition=Disposition.IGNORE,
                reason_code="owner_correction",
            ),
        )
    )

    result = evaluate_relevance(text, SpeakerContext.native_owner("memo-phone"), interests)

    assert result.dispatch_allowed is True
    assert result.reason_code == "owner_explicit_memo_command"

OWNER = SpeakerContext.native_owner("ambient-eval")


@pytest.mark.parametrize("text", ACTIONABLE)
def test_realistic_owner_requests_are_actionable(text: str) -> None:
    result = evaluate_relevance(text, OWNER)
    assert result.dispatch_allowed, (text, result.reason_code)
    assert result.confidence >= 0.85


@pytest.mark.parametrize("text", ORDINARY)
def test_realistic_ordinary_conversation_does_not_dispatch(text: str) -> None:
    result = evaluate_relevance(text, OWNER)
    assert not result.dispatch_allowed, (text, result.primary_category)


def test_ambient_eval_precision_and_recall_are_perfect_on_named_suite() -> None:
    labeled = [(text, True) for text in ACTIONABLE] + [(text, False) for text in ORDINARY]
    predictions = [(evaluate_relevance(text, OWNER).dispatch_allowed, expected) for text, expected in labeled]
    true_positive = sum(predicted and expected for predicted, expected in predictions)
    false_positive = sum(predicted and not expected for predicted, expected in predictions)
    false_negative = sum(not predicted and expected for predicted, expected in predictions)
    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / (true_positive + false_negative)

    assert len(labeled) == 40
    assert precision == 1.0
    assert recall == 1.0
