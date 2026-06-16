"""Parse a messy torrent folder/file name into a clean MovieInfo.

Two-stage approach:
  1. Pre-process: strip known garbage patterns with regex so the LLM gets
     a cleaner input and uses fewer tokens / tool calls.
  2. LLM: Ollama agent with web_search tool resolves the clean title, year,
     and content type. Falls back gracefully if Ollama is unavailable.

Returns a MovieInfo dataclass, or raises ContentSkipped for non-movie content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .client import AIClient


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class MovieInfo:
    title:      str
    year:       int | None
    resolution: str | None   # e.g. "1080p", "720p", "2160p" — from LLM or ffprobe fallback
    raw_name:   str           # original input for reference


class ContentSkipped(Exception):
    """Raised when the input is detected as non-movie content."""
    def __init__(self, reason: str, raw_name: str) -> None:
        super().__init__(reason)
        self.reason   = reason
        self.raw_name = raw_name


# ── Public entry point ────────────────────────────────────────────────────────

def parse(path: Path) -> MovieInfo:
    """Parse a torrent path (folder or file) into a MovieInfo.

    Raises ContentSkipped if the path looks like a TV show, concert, software, etc.
    """
    raw = path.name

    # Hard filters before touching the LLM
    _check_skip(raw)

    cleaned = _preprocess(raw)
    result  = _ask_llm(raw, cleaned)
    return result


# ── Pre-processing ────────────────────────────────────────────────────────────

# Tokens that appear after the title/year and should be stripped
_JUNK_TOKENS = re.compile(
    r"""(?ix)
    \b(
        bluray | blu[-_]?ray | bdrip | bdrip | bdremux |
        web[-_]?dl | web[-_]?rip | webrip | hdrip | dvdrip | dvdscr |
        hdtv | pdtv | ts | cam | scr |
        x264 | x265 | x\.265 | h264 | h\.264 | h265 | h\.265 |
        hevc | avc | xvid | divx |
        10bit | 10[-_]bit | 8bit |
        hdr | hdr10 | dv | dolby[-_]?vision |
        aac | ac3 | dts | ddp | dd5?\.?1 | atmos | truehd | eac3 |
        5\.1 | 7\.1 | 2\.0 |
        remux | proper | repack | extended | theatrical | directors[-_]cut |
        multi | dual[-_]?audio |
        yts\.?(mx|am|lt)? | yify | rarbg | ettv | eztv |
        tigole | bone | galaxyrg | tgx | hazmatt | edge2020 | ddr | oft |
        psarip | psaview | psa | mkvcage | ion10 | mhd | cee | ukbandit |
        anoXmous | eztvx?\.?to | successfulcrab | ethel | ivy |
        1080i | 480i
    )\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

_WWW_PREFIX   = re.compile(r"^www\.\S+\s*-+\s*", re.IGNORECASE)
_DOTS_TO_SPACE = re.compile(r"\.(?=[a-zA-Z0-9])")
_BRACKETS_RES  = re.compile(r"[\[\(](\d{3,4}[pP]|4[kK]|2160[pP])[\]\)]", re.IGNORECASE)
_YEAR_INLINE   = re.compile(r"\b((?:19|20)\d{2})\b")
_TRAILING_JUNK = re.compile(r"[-_\s]+$")
_EXT           = re.compile(r"\.(mkv|mp4|avi|m4v|ts)$", re.IGNORECASE)


def _preprocess(name: str) -> str:
    """Best-effort cleanup before handing to the LLM."""
    s = name

    # Strip file extension
    s = _EXT.sub("", s)

    # Strip www.site.com - prefix
    s = _WWW_PREFIX.sub("", s)

    # Dot-separated names: replace dots that separate words (not decimal points)
    # Only do this when the name has no spaces (pure dot-separated)
    if " " not in s and s.count(".") > 2:
        s = _DOTS_TO_SPACE.sub(" ", s)
        # Re-attach dots that were part of "(1986)" → "( 1986)"
        s = re.sub(r"\( (\d{4}) \)", r"(\1)", s)

    # Normalise resolution tokens: 1080P → 1080p, 4K → 2160p
    s = re.sub(r"\b4[kK]\b", "2160p", s)
    s = re.sub(r"(\d{3,4})[Pp]\b", lambda m: f"{m.group(1)}p", s)

    # Strip known junk tokens
    s = _JUNK_TOKENS.sub(" ", s)

    # Collapse whitespace and trailing punctuation
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = _TRAILING_JUNK.sub("", s)

    return s


# ── Content skip detection ────────────────────────────────────────────────────

_TV_EPISODE = re.compile(r"\bS\d{2}E\d{2}\b", re.IGNORECASE)
_TV_SEASON  = re.compile(r"[\(\[_]s\d{2}[\)\]_]", re.IGNORECASE)
_NON_VIDEO  = re.compile(
    r"\.(flac|mp3|wav|m4a|aiff|ogg|iso|dmg|exe|pdf|epub|fb2|crdownload)$",
    re.IGNORECASE,
)
_SOFTWARE   = re.compile(
    r"\b(repack|portable|appz|build\s+\d|multilingual|final)\b",
    re.IGNORECASE,
)
_BOX_SET    = re.compile(
    r"\b(complete|box\s*set|\d\s*-\s*\d\s+(complete|collection))\b",
    re.IGNORECASE,
)
_CONCERT    = re.compile(
    r"\b(unplugged|live\s+at|live\s+in|concert|mtv\s+unplugged|dvd\s*9|ntsc|pal)\b",
    re.IGNORECASE,
)
_MUSIC      = re.compile(
    r"\.(flac|mp3|wav)$|\b(discography|lossless|vinyl\s*rip|cdm|bootleg|flac)\b",
    re.IGNORECASE,
)


def _check_skip(name: str) -> None:
    """Raise ContentSkipped if the name clearly isn't a movie."""
    if _TV_EPISODE.search(name):
        raise ContentSkipped("TV episode (S##E## pattern)", name)
    if _TV_SEASON.search(name):
        raise ContentSkipped("TV season folder", name)
    if _NON_VIDEO.search(name):
        raise ContentSkipped("Non-video file type", name)
    if _SOFTWARE.search(name):
        raise ContentSkipped("Software / installer", name)
    if _BOX_SET.search(name):
        raise ContentSkipped("Box set / multi-movie collection", name)
    if _CONCERT.search(name):
        raise ContentSkipped("Music concert (use concert pipeline)", name)
    if _MUSIC.search(name):
        raise ContentSkipped("Music / audio content", name)


# ── LLM call ─────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a movie metadata extractor. Given a torrent folder or file name, \
extract the movie title, release year, and video resolution.

You MUST respond with valid JSON only — no explanation, no markdown fences. \
The JSON must have exactly these keys:
  {
    "title":      string,   // clean movie title — Cyrillic for Russian/Ukrainian films, English for all others
    "year":       number or null,
    "resolution": string or null,  // e.g. "1080p", "720p", "2160p", "480p"
    "content_type": "movie" | "tv_show" | "concert" | "software" | "music" | "other"
  }

Rules:
- For Russian and Ukrainian films, output the title in Cyrillic using standard \
  Russian capitalisation (only the first word capitalised, e.g. "8 новых свиданий", \
  "Ирония судьбы, или с лёгким паром!"). For all other non-English films, use the English title.
- Strip all technical garbage: codec names, release group tags, source tags \
  (BluRay, WEBRip, etc.), audio tags (AAC, DTS, AC3), site prefixes (www.xxx.com).
- Dots used as word separators (A.Goofy.Movie) → spaces (A Goofy Movie).
- Year in parens, brackets, or inline after the title — extract it.
- Resolution: look for 480p, 576p, 720p, 1080p, 2160p, 4K (→ "2160p").
- If year or resolution is missing or unclear, use web_search to look it up.
- Use web_search for any title you are not certain about.
- content_type must be "movie" unless clear evidence of another type.
- Correct obvious torrent misspellings (e.g. "Dalmations" → "Dalmatians").
"""


def _ask_llm(raw: str, cleaned: str) -> MovieInfo:
    client = AIClient()

    message = (
        f'Original torrent name: "{raw}"\n'
        f'Pre-processed: "{cleaned}"\n\n'
        "Extract the movie title, year, and resolution. "
        "Use web_search if you are unsure about the title or year. "
        "Return JSON only."
    )

    reply = client.call(system=_SYSTEM, message=message)

    # Strip markdown fences if the model added them despite instructions
    reply = re.sub(r"^```[a-z]*\s*", "", reply.strip(), flags=re.IGNORECASE)
    reply = re.sub(r"\s*```$", "", reply.strip())

    try:
        data = json.loads(reply)
    except json.JSONDecodeError:
        # Try to extract JSON object from mixed output
        m = re.search(r"\{.*\}", reply, re.DOTALL)
        if not m:
            raise ValueError(f"LLM returned non-JSON output:\n{reply}")
        data = json.loads(m.group())

    content_type = data.get("content_type", "movie")
    if content_type != "movie":
        raise ContentSkipped(
            f"LLM classified as '{content_type}', not a movie", raw
        )

    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError(f"LLM returned empty title for: {raw}")

    year = data.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None

    resolution = (data.get("resolution") or "").strip() or None
    if resolution:
        resolution = resolution.lower()

    return MovieInfo(title=title, year=year, resolution=resolution, raw_name=raw)
