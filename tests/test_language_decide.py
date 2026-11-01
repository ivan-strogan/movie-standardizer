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
    assert r.language == "other"


def test_weak_margin_is_inconclusive():
    r = decide([(0.55, 0.40), (0.50, 0.45), (0.52, 0.43)])
    assert r.language == "" and "margin" in r.reason


def test_low_mass_clips_are_ignored_not_counted_against():
    # one clip is music/other, three are clearly Russian
    r = decide([(0.02, 0.05), (0.0, 0.98), (0.01, 0.97), (0.0, 0.99)])
    assert r.language == "rus" and r.clips_used == 3


def test_clearly_other_language_is_marked_other_not_inconclusive():
    r = decide([(0.02, 0.01), (0.03, 0.0), (0.01, 0.02), (0.04, 0.01)])
    assert r.language == "other"


def test_two_other_language_clips_is_only_inconclusive():
    assert decide([(0.02, 0.01), (0.03, 0.0)]).language == ""


def test_mostly_foreign_with_a_few_english_clips_is_other():
    # a dub in another language that picks up a few English lines
    foreign = [(0.02, 0.01)] * 20
    r = decide(foreign + [(0.95, 0.0)] * 3)
    assert r.language == "other"


def test_english_with_some_foreign_clips_stays_english():
    r = decide([(0.98, 0.0)] * 15 + [(0.02, 0.01)] * 3)
    assert r.language == "eng"
