from pathlib import Path

from movie_standardizer.media.audio import build_audio_args, mark_default, select_audio_tracks
from movie_standardizer.media.labels import movie_language
from movie_standardizer.media.streams import AudioStream, ProbeResult, VideoStream


def _audio(index, lang, codec="ac3", channels=6):
    return AudioStream(index=index, codec=codec, channels=channels, language=lang, title="",
                       bitrate=0, is_default=index == 1, sample_rate=48000)


def _tracks(*audio):
    video = VideoStream(0, "h264", 1920, 1080, "23.976", 8, True)
    return select_audio_tracks(ProbeResult(Path("x.mkv"), video, list(audio), [], 100.0, 1))


def _defaults(tracks):
    return [t.is_default for t in tracks]


def test_movie_language_from_title():
    assert movie_language("Some English Title") == "eng"
    assert movie_language("Брат 2") == "rus"
    assert movie_language("") == "eng"


def test_english_movie_defaults_to_english_even_when_russian_is_first():
    tracks = _tracks(_audio(1, "rus"), _audio(2, "eng"))
    mark_default(tracks, "Some English Title")
    assert _defaults(tracks) == [False, True]


def test_russian_movie_defaults_to_russian():
    tracks = _tracks(_audio(1, "eng"), _audio(2, "rus"))
    mark_default(tracks, "Брат 2")
    assert _defaults(tracks) == [False, True]


def test_only_first_track_in_movie_language_is_default():
    tracks = _tracks(_audio(1, "rus"), _audio(2, "eng"), _audio(3, "eng", "aac", 2))
    mark_default(tracks, "Some English Title")
    assert _defaults(tracks) == [False, True, False]


def test_no_track_in_movie_language_falls_back_to_first():
    tracks = _tracks(_audio(1, "rus"), _audio(2, "rus"))
    mark_default(tracks, "Some English Title")
    assert _defaults(tracks) == [True, False]


def test_single_track_stays_default():
    tracks = _tracks(_audio(1, "eng"))
    mark_default(tracks, "Some English Title")
    assert _defaults(tracks) == [True]


def test_ffmpeg_args_flag_exactly_one_default():
    tracks = _tracks(_audio(1, "rus"), _audio(2, "eng"))
    mark_default(tracks, "Some English Title")
    args = build_audio_args(tracks)
    flags = [args[i + 1] for i, a in enumerate(args) if a.startswith("-disposition:a:")]
    assert flags == ["0", "default"]


def test_ffmpeg_args_default_first_when_nothing_flagged():
    tracks = _tracks(_audio(1, "eng"), _audio(2, "rus"))
    for t in tracks:
        t.is_default = False
    args = build_audio_args(tracks)
    flags = [args[i + 1] for i, a in enumerate(args) if a.startswith("-disposition:a:")]
    assert flags == ["default", "0"]
