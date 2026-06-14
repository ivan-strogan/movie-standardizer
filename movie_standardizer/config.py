"""Central configuration for movie-standardizer."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── NAS paths ─────────────────────────────────────────────────────────────────

TORRENTS_DIR = Path(
    os.environ.get("TORRENTS_DIR", "/Volumes/Torrents/3TB Mirror/Torrents")
)
OUTPUT_DIR = Path(
    os.environ.get("OUTPUT_DIR", "/Volumes/video/Movies_AV1")
)

# ── Project paths ─────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent.parent
LOGS_DIR    = PROJECT_DIR / "logs"

# ── ffmpeg binaries ───────────────────────────────────────────────────────────

FFMPEG_BIN  = "ffmpeg"
FFPROBE_BIN = "ffprobe"

# ── SVT-AV1 encoding settings ─────────────────────────────────────────────────

VIDEO_CODEC   = "libsvtav1"
PRESET        = 5
SVTAV1_PARAMS = "tune=0"

# ── CRF selection (same logic as movie-av1-converter) ────────────────────────

CRF_BITRATE_TIERS = [
    (500,  38),
    (1000, 34),
    (2000, 30),
    (4000, 26),
    (8000, 22),
]
CRF_BITRATE_MAX = 20

CRF_CODEC_OFFSET = {
    "hevc": +6,
    "av1":  +6,
    "vp9":  +4,
    "vp8":  +2,
    "mpeg4":      -2,
    "msmpeg4v3":  -2,
    "msmpeg4v2":  -2,
    "msmpeg4":    -2,
    "mpeg2video": -2,
    "mpeg1video": -2,
    "wmv1": -2,
    "wmv2": -2,
    "wmv3": -2,
}

CRF_MIN = 18
CRF_MAX = 51

# Probe-encode: 10 x 1-min clips, target ratio ≤ this before full encode.
PROBE_TARGET_RATIO = 0.82

# ── Audio settings ────────────────────────────────────────────────────────────

# Codecs considered "already surround" — stream copy, no AC3 encode needed.
SURROUND_PASSTHROUGH = {"ac3", "eac3", "dts", "truehd", "mlp", "dts-hd", "dca"}

# Codecs to convert to AC3 when they are 5.1+ channels (regardless of language tag).
# Unknown/untagged (und) tracks are treated the same as any other — kept and converted.
AAC_TRANSCODE_CODECS = {"aac", "mp3", "vorbis", "opus"}

AC3_BITRATE = "640k"

# Only drop a track when it is a provable exact duplicate:
# same codec + same language tag + same channel count already seen in the file.
# Never drop based on language tag alone — most torrents leave language as 'und'.

# ── Source done marker ────────────────────────────────────────────────────────

SOURCE_DONE_SUFFIX = "_done"

# ── Ollama / AI settings ──────────────────────────────────────────────────────

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

# ── Subtitle handling ─────────────────────────────────────────────────────────

# Subtitle codecs that must be transcoded when muxing into MKV.
SUB_TRANSCODE = {
    "mov_text": "srt",
}
