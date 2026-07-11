from followthrough.classifier import classify


def test_noise_is_discarded():
    result = classify('Lunch was great; the sandwich was perfect.')
    assert result.actionable is False
    assert result.kind == 'ordinary_life'


def test_business_signal_is_kept():
    assert classify('Research the Hermes startup and follow up with Maya.').actionable is True

