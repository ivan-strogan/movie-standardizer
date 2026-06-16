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
from ..media.audio import select_audio_tracks
from ..media.encoder import encode
from ..media.streams import analyze
from .. import config


# ── Video file extensions we process ─────────────────────────────────────────

_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}


# ── Public entry point ────────────────────────────────────────────────────────

def run(source: Path, dry_run: bool = False) -> bool:
    """Process one source path (folder or file) through the full pipeline.

    Returns True on success, False on failure or skip.
    """
    print(f"\nSource: {source.name}")

    # Stage 1: locate video file
    video_file = _find_video(source)
    if video_file is None:
        print("  SKIP: no video file found")
        return False

    # Stage 2: parse name with AI
    print("  Parsing name ...")
    try:
        movie_info = parse(source)
    except ContentSkipped as e:
        print(f"  SKIP: {e.reason}")
        return False
    except Exception as e:
        print(f"  ERROR parsing name: {e}")
        return False

    print(f"  Title:      {movie_info.title}")
    print(f"  Year:       {movie_info.year or '(unknown)'}")
    print(f"  Resolution: {movie_info.resolution or '(from file)'}")

    # Stage 3: probe streams
    print("  Probing streams ...")
    try:
        probe_result = analyze(video_file)
    except Exception as e:
        print(f"  ERROR probing {video_file.name}: {e}")
        return False

    print(f"  Video:  {probe_result.video.codec} {probe_result.resolution} "
          f"{probe_result.video.fps}fps {probe_result.video.bit_depth}bit")
    for a in probe_result.audio:
        print(f"  Audio:  {a.codec} {a.channels}ch lang={a.language!r} "
              f"title={a.title!r}  -> {a.action}")
    print(f"  Subs:   {len(probe_result.subtitles)} track(s)")

    # Stage 4: select audio tracks
    audio_tracks = select_audio_tracks(probe_result)

    print("  Audio output plan:")
    for t in audio_tracks:
        action = f"{t.codec_arg}"
        if t.bitrate_arg:
            action += f" {t.bitrate_arg}"
        print(f"    [{t.source_index}] {action} lang={t.language!r} title={t.title!r}")

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

    if dry_run:
        print("  [dry-run] encode command:")
        encode(probe_result, audio_tracks, job.output_file, dry_run=True)
        return True

    # Check output doesn't already exist
    if job.output_file.exists():
        print(f"  SKIP: output already exists: {job.output_file}")
        return False

    # Stage 6: encode
    print()
    success = encode(probe_result, audio_tracks, job.output_file)

    if not success:
        return False

    # Stage 7: mark source as done
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
