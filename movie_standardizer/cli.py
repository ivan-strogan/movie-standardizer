"""CLI entry point.

Usage:
  python main.py <path>            -- process one movie
  python main.py --dry-run <path>  -- show plan without encoding
"""

from __future__ import annotations

import sys
from pathlib import Path

from .pipeline.runner import run


def main() -> None:
    args = sys.argv[1:]

    dry_run = "--dry-run" in args
    args    = [a for a in args if a != "--dry-run"]

    if not args:
        print("Usage: python main.py [--dry-run] <movie-path>", file=sys.stderr)
        print()
        print("  <movie-path>  Torrent folder or video file to process")
        print("  --dry-run     Show the plan and ffmpeg command without encoding")
        sys.exit(1)

    source = Path(args[0])
    if not source.exists():
        print(f"Error: path does not exist: {source}", file=sys.stderr)
        sys.exit(1)

    success = run(source, dry_run=dry_run)
    sys.exit(0 if success else 1)
