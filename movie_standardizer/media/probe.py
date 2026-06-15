"""ffprobe wrapper — returns raw stream and format data for a media file."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from .. import config

_cache: dict[str, dict] = {}


def probe(path: Path) -> Optional[dict]:
    """Run ffprobe on path and return the parsed JSON, or None on failure.

    Result is cached so repeated calls on the same path are free.
    """
    key = str(path)
    if key in _cache:
        return _cache[key]

    cmd = [
        config.FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        _cache[key] = data
        return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def clear_cache() -> None:
    _cache.clear()
