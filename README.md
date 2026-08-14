# Movie Standardizer

Takes movies from a Torrents folder, standardizes the name and format, and outputs a converted copy into `Movies_AV1`.

## Architecture

```
Browser (localhost:8081)
|
|  REST calls                        WebSocket /ws
|  /api/torrents                     |
|  /api/torrents/classify            real-time job updates
|  /api/torrents/classify-others     (progress %, ETA, phase)
|  /api/inspect
|  /api/jobs
v
FastAPI server (server.py)
|
+-- CLASSIFICATION FLOW (/api/torrents/classify)
|   |
|   +-- 1. DB cache check (SQLite)
|   |       cached? -> return immediately
|   |       unknown/uncached -> continue
|   |
|   +-- 2. Pre-filter (_check_skip regex)
|   |       S01E02, .dmg, [PDF], etc -> not_movie, no AI needed
|   |
|   +-- 3. Call 1  (Ollama, chunks of 50)
|   |       system: "sort into movies / not_movies / unsure"
|   |       user:   {"0": "Tarzan (1999)...", "1": "Backstreet Boys..."}
|   |       reply:  {"movies": [0], "not_movies": [1], "unsure": [...]}
|   |
|   +-- 4. Call 2 for unsure only  (Ollama, chunks of 20)
|           DuckDuckGo search (1 query per item, 15s timeout)
|           system: "given search results, which are movies?"
|           user:   "[0] Name: Blue - All Rise\nSearch results: ..."
|           reply:  [7, 12]   <- just the movie indices
|
+-- INSPECT FLOW (/api/inspect)
|   |
|   +-- ffprobe -> video/audio/subtitle streams
|   +-- Ollama name parser -> {title, year, resolution}
|   +-- audio rules -> which tracks to copy / encode to AC3
|
+-- ENCODE QUEUE (background thread)
        queued -> probing -> crf_tune -> encoding -> done
        ffmpeg CRF probe (10 x 1-min clips to find optimal CRF)
        ffmpeg full AV1 encode (SVT-AV1)
        -> /Volumes/video/Movies_AV1/Title (Year) [res] AC3/

SQLite DB  (classifications.db)
    name -> {category, size_gb, manual}
    cache survives server restarts; Rescan wipes non-manual entries
```

## What it does

1. **Parses the movie name** — uses a local Ollama LLM with a web search tool to extract a clean title, year, and resolution from messy torrent folder/file names. The LLM handles all the naming chaos that regex alone can't reliably solve.

2. **Analyzes streams** — probes the source file with ffprobe, classifies every audio, video, and subtitle track.

3. **Processes audio** — see the full audio rules below.

4. **Encodes video to AV1** — uses SVT-AV1 via ffmpeg. Before the full encode, extracts 10 one-minute clips spread across the film to find the optimal CRF that guarantees the output is smaller than the source. The full encode (video + audio + subs in one pass) then runs with that CRF.

5. **Outputs a standardized MKV** named after the movie:
   - `Movie Title (Year) [resolution]` — when main audio is AAC
   - `Movie Title (Year) [resolution] AC3` — when main audio is AC3
   - `Movie Title (Year) [resolution] DTS` — when main audio is DTS
   - `Movie Title (Year) [resolution] TrueHD` — when main audio is TrueHD

6. **Marks the source** — after a successful conversion the torrent folder/file is renamed with a `_done` suffix.

## Output location

```
/Volumes/video/Movies_AV1/Tarzan (1999) [1080p] AC3/
    Tarzan (1999) [1080p] AC3.mkv
```

---

## Torrent naming patterns handled

The Torrents folder contains a wide variety of naming conventions. The AI name parser is designed to handle all of them.

### Pattern 1 — Parentheses/brackets with year in parens (most common)
```
Tarzan (1999) (1080p BluRay x265 HEVC 10bit AAC 5.0 Tigole)
Airheads (1994) [BluRay] [1080p] [YTS.AM]
My Spy (2020) [1080p] [BluRay] [5.1] [YTS.MX]
Wonder Woman 1984 (2020) [1080p] [BluRay] [5.1] [YTS.MX]
Problem Child 2 (1991) [BluRay] [1080p] [YTS.AM]
Slums of Beverly Hills (1998) [1080p]
Zack Snyders Justice League (2021) [1080p] [WEBRip] [5.1] [YTS.MX]
```
→ Extract: title from text before year, year from `(YYYY)`, resolution from `[###p]`

### Pattern 2 — Year in square brackets
```
Jingle All The Way [1996] 1080p BluRay HEVC x265 10Bit DTS AC3 (UKBandit)
```
→ Same as above but year is `[YYYY]` not `(YYYY)`

### Pattern 3 — Dot-separated, year inline (no parens)
```
A.Charlie.Brown.Christmas.1965.1080p.BluRay.DDP.5.1.H.265-EDGE2020.mkv
A.Goofy.Movie.1995.1080P.BluRay.MHD.X264.DD.5.1-DDR.mkv
Blind.Date.1987.720p.BluRay.999MB.HQ.x265.10bit-GalaxyRG[TGx]
Margin.Call.2011.720p.BluRay.999MB.HQ.x265.10bit-GalaxyRG[TGx]
The.Adam.Project.2022.1080p.WEBRip.x265-RARBG
The.Day.the.Earth.Stood.Still.2008.1080p.BluRay.10bit.x265-HazMatt.mkv
The.Retirement.Plan.2023.1080p.10bit.WEBRip.6CH.x265.HEVC-PSA
Jingle.All.the.Way.1996.Blu-Ray.CEE.1080p
Dennis The Menace 1993 1080p BuRay H265 5.1 BONE.mp4
```
→ Replace dots with spaces, find a 4-digit year token to split title from metadata

### Pattern 4 — Dot-separated, year still in parens within the dots
```
Black.Moon.Rising.(1986).H265.1080p.DVDRip.EzzRips
```
→ Same as Pattern 3 but year is `.(YYYY).` — strip the surrounding dots

### Pattern 5 — www / site prefix garbage
```
www.Torrenting.com - A Goofy Movie 1995 1080p BluRay x264-OFT
www.UIndex.org    -    A Goofy Movie 1995 1080p BluRay x264-GeneMige
www.UIndex.org    -    Zack Snyders Justice League 2021 1080p BluRay DDP 5 1 10bit H 265-iVy
```
→ Strip everything up to and including the ` - ` separator after the URL

### Pattern 6 — Missing year, missing resolution, or underscores
```
101_Dalmations.mkv                       ← no year, no resolution, underscores
X-Men - Dark Phoenix (2019)              ← no resolution
Zack Snyder's Justice League (2021).mkv ← no resolution
```
→ Replace underscores with spaces; resolution falls back to actual video stream height from ffprobe; missing year looked up via web search

### Pattern 7 — 4K / 2160p resolution
```
The Weeknd X The Dawn FM Experience (2022) [2160p] [4K] [WEB] [5.1] [YTS.MX]
```
→ Normalize: `2160p` or `4K` both map to `[2160p]`

### Pattern 8 — Russian / non-ASCII title
```
_Ирония судьбы, или С легким паром (1976) [576p]
Евровидение История огненной саги_2020_WEB-DLRip
```
→ Passed through to the LLM as-is; the model handles non-Latin titles natively and can web-search them

---

## Content filtering — what gets skipped

These are **not** movies and will be detected and skipped with a clear message:

| Pattern | Example | Detection |
|---|---|---|
| TV episodes | `Andor.S02E10.1080p.WEB.h264-ETHEL` | `S##E##` pattern |
| TV seasons | `Chernobyl_(s01)_1080p` | `(s##)` or `_s##_` pattern |
| Music concerts | `Celine Dion - A New Day Live in Las Vegas [2007].mkv` | Goes to a separate concert pipeline |
| Music / albums | `Alex Clare - The Lateness Of The Hour` | Artist – Album format, no video |
| Software / ISOs | `Adobe Acrobat Pro DC 2022 (x64).dmg` | `.dmg` / `.exe` / `.iso` / `RePack` keywords |
| Audio-only files | `audiocheck.net_pinknoise.wav` | Non-video file extension |
| PDF / ebooks | `Mastering Autodesk Revit MEP 2014 [PDF]` | `[PDF]` keyword |
| Box sets | `Fast and Furious 1 - 6 COMPLETE Box Set.1080p` | "COMPLETE" / "Box Set" / numeric range `1 - 6` |

---

## Audio stream handling

The goal is to preserve every meaningful audio track regardless of language, and convert AAC surround to AC3 for maximum compatibility.

### Rules

| Source track | Language | Channels | Action |
|---|---|---|---|
| AAC | any (including `und`) | 6+ (5.1 or above) | Encode → AC3 5.1 @ 640 kbps, keep same language tag |
| AAC | any (including `und`) | 2 (stereo) | Stream copy — kept as a stereo fallback |
| AAC | any (including `und`) | 1 (mono) | Stream copy — kept |
| AC3 / EAC3 | any (including `und`) | any | Stream copy, no re-encode |
| DTS / DTS-HD | any (including `und`) | any | Stream copy, no re-encode |
| TrueHD / MLP | any (including `und`) | any | Stream copy, no re-encode |
| Exact duplicate: same codec + same language tag + same channel count | — | — | Drop the second copy only |

**Unknown / untagged language tracks are kept.** Most torrents don't bother setting language tags correctly, so an `und` track is just as likely to be the main audio as a tagged one. We never drop a track just because its language is unspecified.

The only tracks dropped are provable exact duplicates — same codec, same language, same channel count appearing more than once in the same file.

### Language is preserved as-is

A Russian film with Russian AAC 5.1 as the main track gets Russian AC3 5.1 as the output — the language tag stays `rus`. A film with both Russian and English audio keeps both, each converted/copied according to the rules above. The "main" audio (default flag) follows whichever track had the default flag in the source.

### Folder name audio suffix

The suffix added to the output folder name is based on the **best surround track** in the output:

| Best surround track | Folder suffix |
|---|---|
| AC3 (original or converted from AAC) | ` AC3` |
| EAC3 (Dolby Digital Plus) | ` EAC3` |
| DTS | ` DTS` |
| DTS-HD | ` DTS-HD` |
| TrueHD | ` TrueHD` |
| Stereo AAC only (no 5.1 source) | *(no suffix)* |

### Examples

| Source audio | Output audio | Folder suffix |
|---|---|---|
| AAC 5.1 eng + AAC 2ch eng | AC3 5.1 eng + AAC 2ch eng | ` AC3` |
| AAC 5.1 rus + AAC 2ch rus | AC3 5.1 rus + AAC 2ch rus | ` AC3` |
| AAC 5.1 eng + AAC 5.1 rus | AC3 5.1 eng + AC3 5.1 rus | ` AC3` |
| DTS 5.1 eng + AAC 2ch eng | DTS 5.1 eng + AAC 2ch eng | ` DTS` |
| AC3 5.1 eng | AC3 5.1 eng | ` AC3` |
| AAC 2ch eng only | AAC 2ch eng | *(none)* |

---

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- ffmpeg + ffprobe with SVT-AV1 support (`brew install ffmpeg`)
- [Ollama](https://ollama.com) running locally with `qwen3:8b` pulled

```bash
ollama pull qwen3:8b
```

## Setup

```bash
git clone git@github.com:ivanstrogan/movie-standardizer.git
cd movie-standardizer
uv sync
cp .env.example .env   # edit if your paths differ
```

## Usage

### Web UI (recommended)

```bash
uv run python main.py --web
```

Opens http://localhost:8081 automatically. Requires Ollama to be running.

#### How to use the web UI

1. **Torrents panel** — loads on startup. The AI scans all items in your Torrents folder and groups them: Movies at the top, then TV Shows, Concerts, Music, Software, Other, and Done (already converted). AI classification runs in the background; a Pac-Man animation shows while it's thinking.

2. **Correct a misclassification** — use the "Move to..." dropdown next to any movie to reassign it to a different category (or the "-> Movie" button on any non-movie to promote it). Manual corrections are saved to the DB and survive future re-scans.

3. **Inspect a movie** — click **Inspect** on any movie row (or paste a path into the "Add movie" box and click Inspect). This runs ffprobe + AI name parse and opens a review card showing:
   - Editable **Title** and **Year** fields
   - Every **audio stream** with a language selector and action (Copy / Encode -> AC3 / Exclude)
   - Every **subtitle track** with include/exclude toggle
   - A live **output filename preview** that updates as you edit

4. **Add to queue** — once you're happy with the review, click **Add to Queue ->**. Repeat for as many movies as you want.

5. **Start encoding** — click **Start Queue**. Each job runs through three stages shown with progress bars:
   - **Probe** — ffprobe stream analysis
   - **CRF Tune** — 10 sample clips encoded to find the optimal CRF (skipped for files under 20 min)
   - **Encode** — full AV1 encode with percentage and ETA

6. **Stop / skip** — **Stop after current** finishes the active job then pauses. **Stop Now** kills the active ffmpeg immediately.

7. **Completed** — finished jobs appear in a Completed section with elapsed time and output size. **Show in Finder** reveals the output file. Errored jobs have a **Retry** button.

8. **Rescan** — clears the AI classification cache and re-runs the AI on everything. Manual overrides (from step 2) are preserved unless you choose to wipe everything.

9. **Dry run** — enable the **Dry run** toggle before adding jobs to queue. The CRF probe still runs but the full encode is skipped; output shows what would have been produced.

### CLI

```bash
# Convert a single movie (folder or file path)
uv run python main.py "/Volumes/Torrents/3TB Mirror/Torrents/Tarzan (1999) (1080p BluRay x265 HEVC 10bit AAC 5.0 Tigole)"

# Dry run — shows parsed name, stream plan, and ffmpeg command without encoding
uv run python main.py --dry-run "/Volumes/Torrents/3TB Mirror/Torrents/Tarzan (1999) ..."

# Fix naming issues in the output library
uv run python main.py --fix-library
uv run python main.py --fix-library --dry-run   # preview without renaming
```

## Project structure

```
movie_standardizer/
├── config.py          — paths, encoding settings, audio rules
├── ai/
│   ├── client.py      — Ollama agent client
│   ├── tools.py       — web_search tool (DuckDuckGo)
│   └── name_parser.py — parse torrent name -> {title, year, resolution}
├── media/
│   ├── probe.py       — ffprobe wrapper
│   ├── streams.py     — stream classification
│   └── encoder.py     — AV1 CRF probe + full encode
├── pipeline/
│   ├── job.py         — Job dataclass
│   └── runner.py      — full pipeline orchestration
└── web/
    ├── server.py      — FastAPI server + AI classification endpoints
    ├── db.py          — SQLite cache for AI classifications
    └── static/
        └── index.html — single-page web UI
main.py                — CLI entry point
```

## Naming convention (matches existing Movies_AV1 library)

| Folder name | Audio situation |
|---|---|
| `Tarzan (1999) [1080p] AC3` | AAC 5.1 converted to AC3 |
| `Иван Васильевич меняет профессию (1973) [1080p] AC3` | Russian AAC 5.1 converted to AC3 |
| `The Dark Knight (2008) [1080p] DTS` | DTS original, stream-copied |
| `A Bug's Life (1998) [720p]` | Stereo AAC only, no 5.1 conversion |
| `Wonder Woman 1984 (2020) [2160p] AC3` | 4K source, AC3 converted |
