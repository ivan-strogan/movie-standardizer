"""Audio stream selection and ffmpeg argument builder.

Given a ProbeResult, produces the ffmpeg -map and -c:a arguments needed to:
  - Encode AAC 5+ channel tracks to AC3 5.1 at 640kbps
  - Stream-copy stereo/mono tracks
  - Stream-copy passthrough tracks (AC3, DTS, TrueHD, EAC3)
  - Skip AAC surround when a passthrough surround already covers the same
    language slot (avoids double-encoding when a file already has AC3+AAC)

The output is a list of AudioOutputTrack objects consumed by the encoder.
"""

from __future__ import annotations

from dataclasses import dataclass

from .streams import AudioStream, ProbeResult
from .. import config


# ── Output track descriptor ───────────────────────────────────────────────────

@dataclass
class AudioOutputTrack:
    """Describes one audio track in the output file."""
    source_index: int       # ffprobe stream index in the source file
    codec_arg:    str       # ffmpeg -c:a value: "copy" or "ac3"
    bitrate_arg:  str       # ffmpeg -b:a value: "" for copy, "640k" for ac3
    title:        str       # metadata title tag to set (may be empty)
    language:     str       # metadata language tag (may be empty)
    is_default:   bool


# ── Public entry point ────────────────────────────────────────────────────────

def select_audio_tracks(result: ProbeResult) -> list[AudioOutputTrack]:
    """Return the ordered list of audio tracks for the output file.

    Rules (applied in order):
    1. Collect all passthrough surround tracks (AC3/DTS/TrueHD/EAC3).
    2. For each AAC surround track: if a passthrough surround track already
       exists with the same language (or both are untagged), skip the AAC --
       the passthrough already covers that slot. Otherwise encode to AC3.
    3. Keep all stereo/mono tracks as copy regardless of codec.
    4. Preserve original stream order (passthrough first if it came first,
       then converted, then stereo).
    """
    tracks = result.audio

    # Build a set of language slots already covered by passthrough surround
    passthrough_surround_langs: set[str] = set()
    for t in tracks:
        if t.is_passthrough and t.is_surround:
            passthrough_surround_langs.add(t.language)

    output: list[AudioOutputTrack] = []

    for t in tracks:
        if t.is_surround:
            if t.needs_ac3_encode:
                # Skip if a passthrough surround already covers this language
                if t.language in passthrough_surround_langs:
                    continue
                output.append(_make_ac3(t))
            else:
                # Passthrough surround (AC3, DTS, TrueHD, EAC3, ...)
                output.append(_make_copy(t))
        else:
            # Stereo / mono -- always copy
            output.append(_make_copy(t))

    return output


def build_audio_args(output_tracks: list[AudioOutputTrack]) -> list[str]:
    """Build the ffmpeg -map and audio codec/bitrate arguments.

    Returns a flat list of ffmpeg arguments covering all audio streams.
    Stream indices in the output file are assigned in the order of output_tracks.
    """
    args: list[str] = []

    for i, t in enumerate(output_tracks):
        args += ["-map", f"0:{t.source_index}"]

    for i, t in enumerate(output_tracks):
        args += [f"-c:a:{i}", t.codec_arg]
        if t.bitrate_arg:
            args += [f"-b:a:{i}", t.bitrate_arg]
        if t.language:
            args += [f"-metadata:s:a:{i}", f"language={t.language}"]
        if t.title:
            args += [f"-metadata:s:a:{i}", f"title={t.title}"]

    return args


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ac3(t: AudioStream) -> AudioOutputTrack:
    # Keep original title if present, otherwise generate a descriptive one
    if t.title:
        title = t.title
    elif t.language:
        title = f"{t.language.upper()} DD 5.1"
    else:
        title = "DD 5.1"
    return AudioOutputTrack(
        source_index = t.index,
        codec_arg    = "ac3",
        bitrate_arg  = config.AC3_BITRATE,
        title        = title,
        language     = t.language,
        is_default   = t.is_default,
    )


def _make_copy(t: AudioStream) -> AudioOutputTrack:
    return AudioOutputTrack(
        source_index = t.index,
        codec_arg    = "copy",
        bitrate_arg  = "",
        title        = t.title,
        language     = t.language,
        is_default   = t.is_default,
    )
