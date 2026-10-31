from pathlib import Path

from movie_standardizer.media.audio import (
    apply_audio_overrides, build_audio_args, select_audio_tracks,
)
from movie_standardizer.media.streams import AudioStream, ProbeResult, VideoStream


def _audio(index, codec, channels, rate, lang="eng"):
    return AudioStream(index=index, codec=codec, channels=channels, language=lang,
                       title="", bitrate=0, is_default=index == 1, sample_rate=rate)


def _probe(*audio):
    video = VideoStream(0, "h264", 1920, 1080, "23.976", 8, True)
    return ProbeResult(Path("x.mkv"), video, list(audio), [], 100.0, 1)


def _codecs(tracks):
    return [(t.codec_arg, t.bitrate_arg) for t in tracks]


def test_ac3_surround_44k_is_reencoded():
    tracks = select_audio_tracks(_probe(_audio(1, "ac3", 6, 44100)))
    assert _codecs(tracks) == [("ac3", "640k")]


def test_ac3_surround_48k_is_copied():
    tracks = select_audio_tracks(_probe(_audio(1, "ac3", 6, 48000)))
    assert _codecs(tracks) == [("copy", "")]


def test_eac3_44k_is_reencoded_to_ac3():
    tracks = select_audio_tracks(_probe(_audio(1, "eac3", 6, 44100)))
    assert _codecs(tracks) == [("ac3", "640k")]


def test_stereo_ac3_44k_gets_stereo_bitrate():
    tracks = select_audio_tracks(_probe(_audio(1, "ac3", 2, 44100)))
    assert _codecs(tracks) == [("ac3", "192k")]


def test_stereo_aac_44k_is_still_copied():
    tracks = select_audio_tracks(_probe(_audio(1, "aac", 2, 44100)))
    assert _codecs(tracks) == [("copy", "")]


def test_dts_is_never_resampled():
    tracks = select_audio_tracks(_probe(_audio(1, "dts", 6, 44100)))
    assert _codecs(tracks) == [("copy", "")]


def test_unknown_sample_rate_is_left_alone():
    tracks = select_audio_tracks(_probe(_audio(1, "ac3", 6, 0)))
    assert _codecs(tracks) == [("copy", "")]


def test_override_copy_cannot_keep_bad_rate():
    probe = _probe(_audio(1, "ac3", 6, 44100))
    tracks = apply_audio_overrides(probe, [{"source_index": 1, "action": "copy"}])
    assert _codecs(tracks) == [("ac3", "640k")]


def test_aac_surround_encode_forces_48k_arg():
    args = build_audio_args(select_audio_tracks(_probe(_audio(1, "aac", 6, 44100))))
    assert args[args.index("-ar:a:0") + 1] == "48000"
