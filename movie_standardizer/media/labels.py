"""Standard audio track titles: "[Language] [Format] [Channels]".

e.g. "English DD 5.1", "Russian DTS-HD MA 5.1", "English DD+ Atmos 7.1".
The format and channels describe the OUTPUT track, not the source.
"""

from __future__ import annotations

LANG_NAMES = {"eng": "English", "rus": "Russian"}

_FORMAT_NAMES = {
    "ac3": "DD", "eac3": "DD+", "dts": "DTS", "dca": "DTS",
    "truehd": "TrueHD", "mlp": "TrueHD", "aac": "AAC", "mp3": "MP3",
    "opus": "Opus", "vorbis": "Vorbis", "flac": "FLAC",
}

_CHANNEL_LABELS = {1: "1.0", 2: "2.0", 3: "2.1", 4: "4.0", 5: "5.0",
                   6: "5.1", 7: "6.1", 8: "7.1"}


def channel_label(channels: int) -> str:
    return _CHANNEL_LABELS.get(channels, f"{channels}ch")


def format_label(codec: str, profile: str = "") -> str:
    codec = (codec or "").lower()
    p = (profile or "").lower()
    name = _FORMAT_NAMES.get(codec, codec.upper())
    if codec in ("dts", "dca"):
        if "dts:x" in p:
            name = "DTS:X"
        elif "ma" in p.replace("-", " ").split():
            name = "DTS-HD MA"
        elif "hra" in p.replace("-", " ").split():
            name = "DTS-HD HRA"
    if "atmos" in p:
        name += " Atmos"
    return name


def track_title(language: str, codec: str, channels: int, profile: str = "") -> str:
    """Standard audio title, e.g. 'English DD 5.1'."""
    lang = LANG_NAMES.get(language, language.upper() if language else "Unknown")
    return f"{lang} {format_label(codec, profile)} {channel_label(channels)}"
