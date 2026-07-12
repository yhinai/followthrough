from followthrough.classifier import classify


def test_golden_price_check_is_actionable() -> None:
    result = classify(
        "Followthrough, check the current RTX 5080 price on Best Buy and tell me when you're done."
    )
    assert result.actionable is True
    assert result.kind == "web_task"


def test_noise_is_discarded():
    result = classify('Lunch was great; the sandwich was perfect.')
    assert result.actionable is False
    assert result.kind == 'ordinary_life'


def test_business_signal_is_kept():
    assert classify('Research the Hermes startup and follow up with Maya.').actionable is True
