"""Stream classification — turns raw ffprobe output into structured StreamInfo objects.

Classifies every stream in a media file and applies the audio selection rules:
  - AAC 5.1+ → will be encoded to AC3
  - AAC stereo/mono → stream copy
  - AC3 / DTS / TrueHD / etc. → stream copy (passthrough)
  - Exact duplicates (same codec + language + channels) → drop the second one
  - Unknown/untagged language (und / empty) → KEEP, treated same as any other
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .probe import probe
from .. import config


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class VideoStream:
    index:      int
    codec:      str          # e.g. "hevc", "h264", "av1"
    width:      int
    height:     int
    fps:        str          # e.g. "23.976"
    bit_depth:  int          # e.g. 8 or 10
    is_default: bool


@dataclass
class AudioStream:
    index:      int
    codec:      str          # e.g. "aac", "ac3", "dts"
    channels:   int          # 1, 2, 6, 8 …
    language:   str          # iso639 tag or "" for unknown
    title:      str          # stream title tag if present
    bitrate:    int          # kbps, 0 if unknown
    is_default: bool
    profile:    str = ""     # e.g. "Dolby TrueHD + Dolby Atmos"

    @property
    def has_atmos(self) -> bool:
        return "atmos" in self.profile.lower()

    @property
    def is_surround(self) -> bool:
        # 5+ channels covers both 5.0 (no LFE) and 5.1/7.1
        return self.channels >= 5

    @property
    def needs_ac3_encode(self) -> bool:
        """True if this track should be re-encoded to AC3."""
        return (
            self.codec.lower() in config.AAC_TRANSCODE_CODECS
            and self.is_surround
        )

    @property
    def is_passthrough(self) -> bool:
        """True if this track should be stream-copied without re-encoding."""
        return self.codec.lower() in config.SURROUND_PASSTHROUGH

    @property
    def action(self) -> str:
        if self.needs_ac3_encode:
            return "encode->ac3"
        return "copy"


@dataclass
class SubtitleStream:
    index:    int
    codec:    str
    language: str
    title:    str
    is_default: bool


@dataclass
class ProbeResult:
    path:      Path
    video:     VideoStream
    audio:     list[AudioStream]
    subtitles: list[SubtitleStream]

    # Raw format info
    duration_secs: float
    size_bytes:    int

    @property
    def resolution(self) -> str:
        """e.g. '1080p'"""
        return f"{self.video.height}p"

    @property
    def best_audio_codec(self) -> str:
        """The codec name of the best surround track in the output.

        Used to determine the folder name suffix (AC3, DTS, etc.).
        After processing, AAC surround tracks become AC3.
        """
        for a in self.audio:
            if a.is_surround:
                if a.needs_ac3_encode:
                    return "ac3"
                return a.codec.lower()
        return ""

    @property
    def folder_suffix(self) -> str:
        """The suffix to append to the output folder name, e.g. ' AC3'."""
        codec = self.best_audio_codec
        atmos = any(a.has_atmos for a in self.audio if a.is_surround)
        _map = {
            "ac3":    "AC3",
            "eac3":   "EAC3 Atmos" if atmos else "EAC3",
            "dts":    "DTS",
            "dca":    "DTS",
            "truehd": "TrueHD Atmos" if atmos else "TrueHD",
            "mlp":    "TrueHD Atmos" if atmos else "TrueHD",
        }
        label = _map.get(codec, codec.upper() if codec else "")
        return f" {label}" if label else ""


# ── Public entry point ────────────────────────────────────────────────────────

def analyze(path: Path) -> ProbeResult:
    """Probe a media file and return a classified ProbeResult.

    Raises FileNotFoundError if the file doesn't exist.
    Raises ValueError if ffprobe fails or the file has no video stream.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    data = probe(path)
    if data is None:
        raise ValueError(f"ffprobe failed on {path}")

    streams   = data.get("streams", [])
    fmt       = data.get("format", {})

    video     = _pick_video(streams)
    audio     = _classify_audio(streams)
    subtitles = _classify_subtitles(streams)

    duration  = float(fmt.get("duration") or 0)
    size      = int(fmt.get("size") or path.stat().st_size)

    return ProbeResult(
        path=path,
        video=video,
        audio=audio,
        subtitles=subtitles,
        duration_secs=duration,
        size_bytes=size,
    )


# ── Video ─────────────────────────────────────────────────────────────────────

def _pick_video(streams: list[dict]) -> VideoStream:
    """Select the primary video stream (skips cover art / attached pictures)."""
    video_streams = [
        s for s in streams
        if s.get("codec_type") == "video"
        and not s.get("disposition", {}).get("attached_pic")
    ]
    if not video_streams:
        raise ValueError("No video stream found")

    # Prefer the stream with the most pixels (largest resolution)
    primary = max(
        video_streams,
        key=lambda s: (s.get("width", 0) or 0) * (s.get("height", 0) or 0),
    )

    fps_str = primary.get("r_frame_rate") or primary.get("avg_frame_rate") or "0/1"
    fps = _parse_fps(fps_str)

    pix_fmt   = primary.get("pix_fmt", "")
    bit_depth = 10 if "10" in pix_fmt or "10le" in pix_fmt or "10be" in pix_fmt else 8

    return VideoStream(
        index      = primary["index"],
        codec      = primary.get("codec_name", ""),
        width      = primary.get("width", 0) or 0,
        height     = primary.get("height", 0) or 0,
        fps        = fps,
        bit_depth  = bit_depth,
        is_default = bool(primary.get("disposition", {}).get("default")),
    )


def _parse_fps(fps_str: str) -> str:
    """Convert '24000/1001' → '23.976', '25/1' → '25'."""
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/")
            val = int(num) / int(den)
        else:
            val = float(fps_str)
        if val == int(val):
            return str(int(val))
        return f"{val:.3f}"
    except (ValueError, ZeroDivisionError):
        return fps_str


# ── Audio ─────────────────────────────────────────────────────────────────────

def _classify_audio(streams: list[dict]) -> list[AudioStream]:
    """Return all audio streams, with exact duplicates removed.

    Duplicate = same codec + same normalised language + same channel count.
    The first occurrence is kept; subsequent identical ones are dropped.
    Unknown language (und / empty) is kept — never dropped on language alone.
    """
    raw_audio = [s for s in streams if s.get("codec_type") == "audio"]
    seen: set[tuple] = set()
    result: list[AudioStream] = []

    for s in raw_audio:
        codec    = (s.get("codec_name") or "").lower()
        channels = int(s.get("channels") or 0)
        lang     = _norm_lang(s.get("tags", {}).get("language", ""))
        title    = s.get("tags", {}).get("title", "") or ""
        bitrate  = _bitrate_kbps(s)
        default  = bool(s.get("disposition", {}).get("default"))
        profile  = s.get("profile", "") or ""

        key = (codec, lang, channels)
        if key in seen:
            continue   # exact duplicate — drop
        seen.add(key)

        result.append(AudioStream(
            index      = s["index"],
            codec      = codec,
            channels   = channels,
            language   = lang,
            title      = title,
            bitrate    = bitrate,
            is_default = default,
            profile    = profile,
        ))

    return result


def _norm_lang(tag: str) -> str:
    """Normalise language tag: 'und', None, '' all become ''."""
    t = (tag or "").strip().lower()
    return "" if t in ("und", "undefined", "") else t


def _bitrate_kbps(stream: dict) -> int:
    raw = stream.get("bit_rate") or stream.get("tags", {}).get("BPS") or "0"
    try:
        return int(raw) // 1000
    except (ValueError, TypeError):
        return 0


# ── Subtitles ─────────────────────────────────────────────────────────────────

def _classify_subtitles(streams: list[dict]) -> list[SubtitleStream]:
    return [
        SubtitleStream(
            index      = s["index"],
            codec      = s.get("codec_name", ""),
            language   = _norm_lang(s.get("tags", {}).get("language", "")),
            title      = s.get("tags", {}).get("title", "") or "",
            is_default = bool(s.get("disposition", {}).get("default")),
        )
        for s in streams
        if s.get("codec_type") == "subtitle"
    ]
