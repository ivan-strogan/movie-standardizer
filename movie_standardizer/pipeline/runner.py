"""Pipeline runner -- orchestrates all stages for a single movie.

Stages:
  1. Locate the video file inside the source path
  2. Parse the movie name with the AI name parser
  3. Probe streams with ffprobe
  4. Select audio output tracks
  5. Confirm output path with the user (dry-run shows plan)
  6. Encode (video AV1 + audio + subs) to output path
  7. Mark source as done by renaming with _done suffix
"""

from __future__ import annotations

from pathlib import Path

from .job import Job
from ..ai.name_parser import ContentSkipped, MovieInfo, parse
from ..media.audio import apply_audio_overrides, select_audio_tracks
from ..media.encoder import encode
from ..media.streams import analyze
from .. import config


# ── Video file extensions we process ─────────────────────────────────────────

_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}


# ── Public entry point ────────────────────────────────────────────────────────

def run(
    source:         Path,
    dry_run:        bool = False,
    on_progress=None,
    user_overrides: dict | None = None,
) -> bool:
    """Process one source path (folder or file) through the full pipeline.

    user_overrides (from the web UI review step) may contain:
      title, year, resolution, audio (list of override dicts), subtitles (list of override dicts)

    Returns True on success, False on failure or skip.
    """
    def emit(stage: str, pct: float, message: str = "", eta: str = "") -> None:
        if on_progress:
            on_progress(stage=stage, pct=pct, message=message, eta=eta)

    print(f"\nSource: {source.name}")

    # Stage 1: locate video file
    video_file = _find_video(source)
    if video_file is None:
        print("  SKIP: no video file found")
        return False

    # Stage 2: parse name — skip if title already provided via overrides
    overrides = user_overrides or {}
    if overrides.get("title"):
        movie_info = MovieInfo(
            title      = overrides["title"],
            year       = overrides.get("year"),
            resolution = overrides.get("resolution"),
            raw_name   = source.name,
        )
        print(f"  Title (override): {movie_info.title} ({movie_info.year})")
    else:
        print("  Parsing name ...")
        emit("name", 0.0, "Parsing name...")
        try:
            movie_info = parse(source)
        except ContentSkipped as e:
            print(f"  SKIP: {e.reason}")
            emit("name", -1.0, f"SKIP: {e.reason}")
            return False
        except Exception as e:
            print(f"  ERROR parsing name: {e}")
            emit("name", -1.0, f"ERROR: {e}")
            return False
        emit("name", 1.0, f"{movie_info.title} ({movie_info.year})")

    print(f"  Title:      {movie_info.title}")
    print(f"  Year:       {movie_info.year or '(unknown)'}")
    print(f"  Resolution: {movie_info.resolution or '(from file)'}")

    # Stage 3: probe streams
    print("  Probing streams ...")
    emit("probe", 0.0, "Probing streams...")
    try:
        probe_result = analyze(video_file)
    except Exception as e:
        print(f"  ERROR probing {video_file.name}: {e}")
        emit("probe", -1.0, f"ERROR: {e}")
        return False

    emit("probe", 1.0, f"{probe_result.video.codec.upper()} {probe_result.resolution}")
    print(f"  Video:  {probe_result.video.codec} {probe_result.resolution} "
          f"{probe_result.video.fps}fps {probe_result.video.bit_depth}bit")
    for a in probe_result.audio:
        print(f"  Audio:  {a.codec} {a.channels}ch lang={a.language!r} "
              f"title={a.title!r}  -> {a.action}")
    print(f"  Subs:   {len(probe_result.subtitles)} track(s)")

    # Stage 4: select audio tracks (with user overrides if provided)
    audio_overrides = overrides.get("audio")
    if audio_overrides:
        audio_tracks = apply_audio_overrides(probe_result, audio_overrides)
    else:
        audio_tracks = select_audio_tracks(probe_result)

    print("  Audio output plan:")
    for t in audio_tracks:
        action = f"{t.codec_arg}"
        if t.bitrate_arg:
            action += f" {t.bitrate_arg}"
        print(f"    [{t.source_index}] {action} lang={t.language!r} title={t.title!r}")

    subtitle_overrides = overrides.get("subtitles")

    # Stage 5: build job and show output path
    job = Job(
        source_path  = source,
        video_file   = video_file,
        movie_info   = movie_info,
        probe_result = probe_result,
        audio_tracks = audio_tracks,
    )

    print(f"  Output name: {job.output_name}")
    print(f"  Output file: {job.output_file}")

    # Check output doesn't already exist (skip in dry run)
    if not dry_run and job.output_file.exists():
        print(f"  SKIP: output already exists: {job.output_file}")
        return False

    # Stage 6: encode (dry_run runs CRF probe but skips full encode)
    print()
    success = encode(
        probe_result,
        audio_tracks,
        job.output_file,
        dry_run=dry_run,
        on_progress=on_progress,
        subtitle_overrides=subtitle_overrides,
    )

    if not success:
        return False

    # Stage 7: mark source as done (skip in dry run)
    if not dry_run:
        _mark_done(source)
        print(f"  Source marked done: {source.name}{config.SOURCE_DONE_SUFFIX}")
    return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_video(source: Path) -> Path | None:
    """Return the primary video file for a source path.

    If source is a file, return it directly.
    If source is a folder, return the largest video file inside it
    (largest = most likely the main feature, not a featurette).
    """
    if source.is_file():
        if source.suffix.lower() in _VIDEO_EXTS:
            return source
        return None

    candidates = [
        f for f in source.rglob("*")
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTS
    ]
    if not candidates:
        return None

    return max(candidates, key=lambda f: f.stat().st_size)


def _mark_done(source: Path) -> None:
    """Rename the source folder/file with a _done suffix."""
    if source.is_file():
        new_name = source.with_name(source.stem + config.SOURCE_DONE_SUFFIX + source.suffix)
    else:
        new_name = source.with_name(source.name + config.SOURCE_DONE_SUFFIX)
    try:
        source.rename(new_name)
    except OSError as e:
        print(f"  WARNING: could not mark source as done: {e}")
