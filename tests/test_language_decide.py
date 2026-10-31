from movie_standardizer.media.language import decide


def test_clear_english():
    r = decide([(0.98, 0.00), (0.95, 0.01), (0.99, 0.00)])
    assert r.language == "eng" and r.confidence > 0.95 and r.clips_used == 3


def test_clear_russian():
    r = decide([(0.00, 0.97), (0.01, 0.99), (0.00, 0.90), (0.02, 0.95)])
    assert r.language == "rus"


def test_too_few_speech_clips_is_inconclusive():
    r = decide([(0.98, 0.0), (0.97, 0.0)])
    assert r.language == "" and "clip" in r.reason


def test_no_clips_is_inconclusive():
    assert decide([]).language == ""


def test_clips_that_disagree_are_inconclusive():
    r = decide([(0.95, 0.0), (0.0, 0.95), (0.9, 0.0)])
    assert r.language == "" and r.reason == "clips disagree"


def test_other_language_is_not_forced_into_english_or_russian():
    # French speech: neither en nor ru gets meaningful probability
    r = decide([(0.05, 0.02), (0.03, 0.01), (0.06, 0.02)])
    assert r.language == ""


def test_weak_margin_is_inconclusive():
    r = decide([(0.55, 0.40), (0.50, 0.45), (0.52, 0.43)])
    assert r.language == "" and "margin" in r.reason


def test_low_mass_clips_are_ignored_not_counted_against():
    # one clip is music/other, three are clearly Russian
    r = decide([(0.02, 0.05), (0.0, 0.98), (0.01, 0.97), (0.0, 0.99)])
    assert r.language == "rus" and r.clips_used == 3
