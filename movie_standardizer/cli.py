"""CLI entry point.

Usage:
  python main.py <path>            -- process one movie
  python main.py --dry-run <path>  -- show plan without encoding
  python main.py --fix-library     -- fix naming in OUTPUT_DIR
  python main.py --fix-library --dry-run  -- preview fixes without renaming
"""

from __future__ import annotations

import sys
from pathlib import Path

from .pipeline.runner import run
from .pipeline.fixer import scan_library
from . import config


def main() -> None:
    args = sys.argv[1:]

    dry_run     = "--dry-run"     in args
    fix_library = "--fix-library" in args
    args        = [a for a in args if a not in ("--dry-run", "--fix-library")]

    if fix_library:
        scan_library(config.OUTPUT_DIR, dry_run=dry_run)
        sys.exit(0)

    if not args:
        print("Usage: python main.py [--dry-run] <movie-path>", file=sys.stderr)
        print("       python main.py --fix-library [--dry-run]", file=sys.stderr)
        print()
        print("  <movie-path>   Torrent folder or video file to process")
        print("  --dry-run      Show the plan and ffmpeg command without encoding")
        print("  --fix-library  Fix naming issues in the output library")
        sys.exit(1)

    source = Path(args[0])
    if not source.exists():
        print(f"Error: path does not exist: {source}", file=sys.stderr)
        sys.exit(1)

    success = run(source, dry_run=dry_run)
    sys.exit(0 if success else 1)
