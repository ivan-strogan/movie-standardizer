"""Library fixer — scans OUTPUT_DIR and renames non-standard entries.

Strategy per entry:
  1. Regex: extract title / year / resolution from messy name.
  2. Probe: ffprobe the MKV to get the correct audio suffix.
  3. AI fallback: for names that can't be parsed by regex (e.g. Russian,
     dot-separated with no resolution bracket).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..ai.name_parser import parse
from ..media.streams import analyze
from .. import config

_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v"}

# Exact standard format: "Title (Year) [Xp]" + optional " CODEC"
_STANDARD = re.compile(
    r'^(.+) \((\d{4})\) \[(\d{3,4}p)\](?: (AC3|EAC3|DTS|DTS-HD|TrueHD))?$'
)

# Flexible parser: handles parens/brackets/braces for year and resolution,
# missing 'p', no space before bracket, uppercase P, etc.
_FLEXIBLE = re.compile(
    r'^(.+?)\s+'
    r'[\[\(]?(\d{4})[\]\)]?\s*'
    r'[\[\(\{](\d{3,4})[Pp]?[\]\)\}]',
    re.IGNORECASE,
)


def scan_library(output_dir: Path, dry_run: bool = False) -> None:
    """Scan output_dir and fix any non-standard named entries."""
    entries = sorted(
        e for e in output_dir.iterdir()
        if not e.name.startswith(('.', '__'))
        and e.suffix.lower() not in {'.db', '.vsmeta'}
    )

    to_fix = [
        e for e in entries
        if not _STANDARD.match(e.stem if e.is_file() else e.name)
    ]

    if not to_fix:
        print("Library is clean — no naming issues found.")
        return

    label = "[dry-run] " if dry_run else ""
    print(f"Found {len(to_fix)} entries to fix:\n")

    for entry in to_fix:
        _fix_entry(entry, dry_run, label)


def _fix_entry(entry: Path, dry_run: bool, label: str) -> None:
    name = entry.stem if entry.is_file() else entry.name
    print(f"  {name}")

    video_file = _find_video(entry)
    if video_file is None:
        print(f"    SKIP: no video file found")
        return

    m = _FLEXIBLE.match(name)
    if m:
        title = m.group(1).strip()
        year  = m.group(2)
        res   = m.group(3) + 'p'

        try:
            probe_result = analyze(video_file)
            suffix = probe_result.folder_suffix
        except Exception as e:
            print(f"    WARNING: probe failed ({e}), omitting audio suffix")
            suffix = ""

        target_name = f"{title} ({year}) [{res}]{suffix}"
    else:
        print(f"    Using AI parser ...")
        try:
            movie_info = parse(entry)
        except Exception as e:
            print(f"    ERROR: {e}")
            return

        try:
            probe_result = analyze(video_file)
            suffix = probe_result.folder_suffix
        except Exception as e:
            print(f"    WARNING: probe failed ({e}), omitting audio suffix")
            suffix = ""
            probe_result = None

        res = movie_info.resolution or (probe_result.resolution if probe_result else "")
        year_str = f" ({movie_info.year})" if movie_info.year else ""
        target_name = f"{movie_info.title}{year_str} [{res}]{suffix}"

    if target_name == name:
        print(f"    OK: no change needed")
        return

    print(f"    {label}→ {target_name}")

    if not dry_run:
        _rename(entry, video_file, target_name)


def _rename(entry: Path, video_file: Path, target_name: str) -> None:
    parent = entry.parent

    if entry.is_dir():
        new_folder = parent / target_name
        if new_folder.exists():
            print(f"    ERROR: target already exists: {new_folder.name}")
            return
        try:
            entry.rename(new_folder)
        except OSError as e:
            print(f"    ERROR renaming folder: {e}")
            return
        # Rename the MKV inside if its name differs
        moved_video = new_folder / video_file.name
        target_mkv  = new_folder / f"{target_name}.mkv"
        if moved_video != target_mkv and moved_video.exists():
            try:
                moved_video.rename(target_mkv)
            except OSError as e:
                print(f"    WARNING: could not rename file inside folder: {e}")
    else:
        # Loose file: create folder and move file into it
        new_folder = parent / target_name
        if new_folder.exists():
            print(f"    ERROR: target folder already exists: {new_folder.name}")
            return
        try:
            new_folder.mkdir()
            entry.rename(new_folder / f"{target_name}.mkv")
        except OSError as e:
            print(f"    ERROR: {e}")


def _find_video(source: Path) -> Path | None:
    if source.is_file():
        return source if source.suffix.lower() in _VIDEO_EXTS else None
    candidates = [
        f for f in source.rglob("*")
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_size)
