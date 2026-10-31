from pathlib import Path

from movie_standardizer.media.audio import apply_audio_overrides, select_audio_tracks
from movie_standardizer.media.labels import channel_label, format_label, track_title
from movie_standardizer.media.streams import AudioStream, ProbeResult, VideoStream


def _audio(index, codec, channels, rate=48000, lang="eng", profile=""):
    return AudioStream(index=index, codec=codec, channels=channels, language=lang,
                       title="SomeReleaseGroup - junk", bitrate=0, is_default=index == 1,
                       profile=profile, sample_rate=rate)


def _probe(*audio):
    video = VideoStream(0, "h264", 1920, 1080, "23.976", 8, True)
    return ProbeResult(Path("x.mkv"), video, list(audio), [], 100.0, 1)


def test_channel_labels():
    assert [channel_label(n) for n in (1, 2, 6, 7, 8)] == ["1.0", "2.0", "5.1", "6.1", "7.1"]


def test_format_labels():
    assert format_label("ac3") == "DD"
    assert format_label("eac3") == "DD+"
    assert format_label("eac3", "Dolby Digital Plus + Dolby Atmos") == "DD+ Atmos"
    assert format_label("truehd", "Dolby TrueHD + Dolby Atmos") == "TrueHD Atmos"
    assert format_label("dts", "DTS-HD MA") == "DTS-HD MA"
    assert format_label("dts", "DTS") == "DTS"
    assert format_label("aac") == "AAC"


def test_track_title_languages():
    assert track_title("eng", "ac3", 6) == "English DD 5.1"
    assert track_title("rus", "dts", 6, "DTS-HD MA") == "Russian DTS-HD MA 5.1"
    assert track_title("", "ac3", 6) == "Unknown DD 5.1"


def test_aac_surround_encoded_to_ac3_is_titled_as_output():
    t = select_audio_tracks(_probe(_audio(1, "aac", 6, 44100)))[0]
    assert t.title == "English DD 5.1" and t.language == "eng"


def test_aac_71_is_titled_5_1_because_ac3_downmixes():
    t = select_audio_tracks(_probe(_audio(1, "aac", 8)))[0]
    assert t.title == "English DD 5.1"


def test_copied_tracks_keep_real_layout_and_drop_source_title():
    tracks = select_audio_tracks(_probe(
        _audio(1, "dts", 8, profile="DTS-HD MA"), _audio(2, "aac", 2, lang="rus")))
    assert [t.title for t in tracks] == ["English DTS-HD MA 7.1", "Russian AAC 2.0"]


def test_override_language_changes_title_and_tag():
    t = apply_audio_overrides(_probe(_audio(1, "ac3", 6)),
                              [{"source_index": 1, "action": "copy", "language": "rus"}])[0]
    assert t.title == "Russian DD 5.1" and t.language == "rus"


def test_other_language_tracks_are_excluded():
    other = _audio(2, "aac", 2, lang="")
    other.lang_source = "other"
    tracks = select_audio_tracks(_probe(_audio(1, "aac", 6), other))
    assert [t.source_index for t in tracks] == [1]


def test_other_language_surround_does_not_set_folder_suffix():
    dts = _audio(2, "dts", 6, lang="")
    dts.lang_source = "other"
    assert _probe(_audio(1, "aac", 6), dts).best_audio_codec == "ac3"
