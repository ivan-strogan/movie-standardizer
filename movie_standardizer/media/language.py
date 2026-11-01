"""Detect the spoken language of each audio track (English vs Russian).

Source language tags are not trusted. For every audio track we cut short clips
from across the film, keep only clips that contain speech, and run Whisper
language ID on them. A track is tagged only when enough speech clips agree
strongly; otherwise the result is inconclusive and the track is flagged so a
human can confirm it (never silently guessed).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import config
from .labels import movie_language
from .streams import AudioStream

SAMPLE_RATE      = 16000
CLIP_SECS        = 30
WINDOWS          = 12      # candidate clip positions, evenly spread over the film
MAX_CLIPS        = 5       # stop once this many speech clips are collected
MIN_CLIPS        = 3       # fewer speech clips than this -> inconclusive
MIN_SPEECH_SECS  = 15      # a clip counts only if this much of it is speech
MIN_EN_RU_MASS   = 0.5     # a clip must put at least this probability on en+ru
MIN_AGREEMENT    = 0.9     # winner's share of the en+ru probability, summed
MAX_FOREIGN_SHARE = 0.75   # if more than this share of speech clips is neither en nor ru -> "other"


@dataclass
class LanguageResult:
    language:   str          # "eng", "rus", "other" (some other language), or "" if inconclusive
    confidence: float        # winner's share of en+ru probability, 0 if inconclusive
    clips_used: int
    reason:     str = ""     # why it was inconclusive


def decide(clips: list[tuple[float, float]]) -> LanguageResult:
    """Decide from per-clip (p_en, p_ru) probabilities, speech clips only.

    p_ru includes Ukrainian (a Ukrainian track is tagged Russian).
    """
    usable = [(en, ru) for en, ru in clips if en + ru >= MIN_EN_RU_MASS]
    foreign = len(clips) - len(usable)
    if len(clips) >= MIN_CLIPS and foreign > MAX_FOREIGN_SHARE * len(clips):
        return LanguageResult("other", 0.0, len(clips),
                              "speech is mostly neither English nor Russian")
    if len(usable) < MIN_CLIPS:
        return LanguageResult("", 0.0, len(usable),
                              f"only {len(usable)} clip(s) clearly English or Russian")
    winners = {"eng" if en > ru else "rus" for en, ru in usable}
    if len(winners) > 1:
        return LanguageResult("", 0.0, len(usable), "clips disagree")
    en = sum(c[0] for c in usable)
    ru = sum(c[1] for c in usable)
    lang = "eng" if en > ru else "rus"
    share = max(en, ru) / (en + ru)
    if share < MIN_AGREEMENT:
        return LanguageResult("", 0.0, len(usable), f"weak margin ({share:.2f})")
    return LanguageResult(lang, share, len(usable))


_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(config.LANG_DETECT_MODEL, device="cpu",
                              compute_type="int8", cpu_threads=8)
    return _model


def _extract_clip(path: Path, stream_index: int, start: float) -> np.ndarray | None:
    cmd = [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
           "-ss", str(int(start)), "-i", str(path), "-t", str(CLIP_SECS),
           "-map", f"0:{stream_index}", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < SAMPLE_RATE * CLIP_SECS:
        return None
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _speech_secs(audio: np.ndarray) -> float:
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    stamps = get_speech_timestamps(audio, VadOptions())
    return sum(t["end"] - t["start"] for t in stamps) / SAMPLE_RATE


def detect_track_language(path: Path, stream_index: int, duration: float) -> LanguageResult:
    if duration < CLIP_SECS * 2:
        return LanguageResult("", 0.0, 0, "file too short")
    model = _get_model()
    clips: list[tuple[float, float]] = []
    for k in range(WINDOWS):
        start = duration * (0.08 + 0.84 * k / (WINDOWS - 1))
        audio = _extract_clip(path, stream_index, start)
        if audio is None or _speech_secs(audio) < MIN_SPEECH_SECS:
            continue
        _, _, probs = model.detect_language(audio)
        p = dict(probs)
        clips.append((p.get("en", 0.0), p.get("ru", 0.0) + p.get("uk", 0.0)))
        if len(clips) >= MAX_CLIPS:
            break
    if not clips:
        return LanguageResult("", 0.0, 0, "no clips with speech found")
    return decide(clips)


def assign_languages(path: Path, audio: list[AudioStream], duration: float,
                     movie_title: str) -> None:
    """Set .language on each track from the audio itself (in place).

    Inconclusive tracks fall back to the title guess (Cyrillic -> rus, else
    eng) and are marked lang_source="fallback" so they can be reviewed.
    Tracks in some other language get no language and lang_source="other";
    they are excluded from the output (never mislabeled as English/Russian).
    """
    for a in audio:
        result = detect_track_language(path, a.index, duration)
        if result.language == "other":
            a.language, a.lang_source, a.lang_confidence = "", "other", 0.0
        elif result.language:
            a.language, a.lang_source, a.lang_confidence = result.language, "detected", result.confidence
        else:
            a.language, a.lang_source, a.lang_confidence = movie_language(movie_title), "fallback", 0.0
