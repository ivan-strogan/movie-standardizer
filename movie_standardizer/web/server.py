"""FastAPI web server for Movie Standardizer.

Run via: python main.py --web
Opens http://localhost:8080 automatically.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..ai.client import check_ollama as _check_ollama
from ..ai.name_parser import ContentSkipped, _check_skip, parse
from ..media.audio import select_audio_tracks
from ..media.encoder import kill_active as _kill_active_encode
from ..media.streams import analyze
from ..pipeline.runner import _find_video, run as pipeline_run
from .. import config
from . import db as _db

app = FastAPI()

_STATIC_DIR = Path(__file__).parent / "static"

# ── Global state ──────────────────────────────────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_ws_clients: set[WebSocket] = set()
_ws_lock = threading.Lock()

# All jobs keyed by id; job["phase"] is the lifecycle phase
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Ordered list of job_ids waiting to encode
_encode_queue: list[str] = []
_encode_lock = threading.Lock()

_queue_running = False
_stop_requested = False
_encode_thread: threading.Thread | None = None

_LANG_NAMES = {
    "eng": "English", "rus": "Russian", "fra": "French", "deu": "German",
    "spa": "Spanish", "ita": "Italian", "jpn": "Japanese", "kor": "Korean",
    "zho": "Chinese", "por": "Portuguese", "chi": "Chinese", "und": "Unknown",
}

# ── FastAPI lifecycle ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    _db.init_db()


# ── Static files + index ──────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# ── REST: Torrents browser ────────────────────────────────────────────────────

@app.get("/api/torrents")
async def list_torrents() -> dict:
    base = config.TORRENTS_DIR
    if not base.exists():
        return {"error": f"TORRENTS_DIR not found: {base}", "items": []}

    cached = _db.get_all()

    items = []
    for p in sorted(base.iterdir()):
        name = p.name
        if name.startswith(".") or name.startswith("__"):
            continue
        cached_entry = cached.get(name)
        if _is_done(name):
            items.append({
                "name":     name,
                "path":     str(p),
                "size_gb":  cached_entry["size_gb"] if cached_entry else None,
                "category": "done",
                "cached":   cached_entry is not None,
                "original": _undone_name(name),
            })
            continue
        items.append({
            "name":     name,
            "path":     str(p),
            "size_gb":  cached_entry["size_gb"] if cached_entry else None,
            "category": cached_entry["category"] if cached_entry else _categorize(name),
            "cached":   cached_entry is not None,
        })

    return {"items": items}


def _is_done(name: str) -> bool:
    suf = config.SOURCE_DONE_SUFFIX
    if name.endswith(suf):  # folder: Some.Folder_done
        return True
    p = Path(name)          # file: title_done.mkv
    return bool(p.suffix) and p.stem.endswith(suf)


def _undone_name(name: str) -> str:
    suf = config.SOURCE_DONE_SUFFIX
    if name.endswith(suf):  # folder
        return name[:-len(suf)]
    p = Path(name)          # file
    return p.stem[:-len(suf)] + p.suffix


# ── REST: Native folder picker ────────────────────────────────────────────────

@app.post("/api/torrents/classify")
async def classify_torrents(body: dict) -> dict:
    """Call 1: identify movies. Names already in the DB are returned from cache;
    only new/unknown names are sent to AI. New results are saved to DB."""
    names = body.get("names", [])
    if not names:
        return {"movies": [], "thinking": ""}

    cached        = _db.get_all()
    cached_movies = [n for n in names if cached.get(n, {}).get("category") == "movie"]
    uncached      = [n for n in names if n not in cached]

    new_movies: list[str] = []
    thinking = ""
    if uncached:
        new_movies, thinking = await asyncio.get_event_loop().run_in_executor(
            None, _classify_movies, uncached
        )
        new_not_movies = [n for n in uncached if n not in new_movies]
        _db.set_many({n: "movie"     for n in new_movies})
        _db.set_many({n: "not_movie" for n in new_not_movies})

    return {"movies": cached_movies + new_movies, "thinking": thinking}


@app.post("/api/torrents/classify-others")
async def classify_others_endpoint(body: dict) -> dict:
    """Categorize non-movie items. Returns cached results for known names;
    only new/unknown names are sent to AI. New results are saved to DB."""
    names = body.get("names", [])
    if not names:
        return {"categories": {}}

    cached = _db.get_all()
    cached_cats = {
        n: cached[n]["category"]
        for n in names
        if n in cached and cached[n]["category"] not in ("movie", "not_movie")
    }
    uncached = [n for n in names if n not in cached or cached[n]["category"] == "not_movie"]

    new_cats: dict[str, str] = {}
    if uncached:
        new_cats = await asyncio.get_event_loop().run_in_executor(
            None, _classify_others, uncached
        )
        _db.set_many(new_cats)

    return {"categories": {**cached_cats, **new_cats}}


@app.post("/api/torrents/category")
async def set_torrent_category(body: dict) -> dict:
    """Persist a manual category change. manual=True means AI re-runs will never overwrite it."""
    name     = body.get("name", "").strip()
    category = body.get("category", "").strip()
    if not name or not category:
        return JSONResponse({"error": "name and category required"}, status_code=400)
    _db.set_category(name, category, manual=True)
    return {"ok": True}


@app.post("/api/torrents/done/delete")
async def delete_done_item(body: dict) -> dict:
    path = body.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    p = Path(path)
    torrents_dir = str(config.TORRENTS_DIR)
    if not str(p).startswith(torrents_dir):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not _is_done(p.name):
        return JSONResponse({"error": "not a done item"}, status_code=400)
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if p.is_dir():
        shutil.rmtree(str(p))
    else:
        p.unlink()
    return {"ok": True}


@app.post("/api/torrents/done/redo")
async def redo_done_item(body: dict) -> dict:
    path = body.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    p = Path(path)
    torrents_dir = str(config.TORRENTS_DIR)
    if not str(p).startswith(torrents_dir):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not _is_done(p.name):
        return JSONResponse({"error": "not a done item"}, status_code=400)
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    original = p.parent / _undone_name(p.name)
    if original.exists():
        return JSONResponse({"error": f"already exists: {original.name}"}, status_code=409)
    p.rename(original)
    return {"ok": True, "original": original.name}


@app.get("/api/db/status")
async def db_status() -> dict:
    try:
        entries = _db.get_all()
        return {"ok": True, "path": str(_db._DB_PATH), "count": len(entries)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/db/reset")
async def db_reset(body: dict) -> dict:
    full = bool(body.get("full", False))
    deleted = _db.reset_all() if full else _db.reset_auto()
    return {"ok": True, "deleted": deleted}


@app.get("/api/ollama/status")
async def ollama_status() -> dict:
    try:
        await asyncio.get_event_loop().run_in_executor(None, _check_ollama)
        return {"ok": True, "model": config.OLLAMA_MODEL, "error": ""}
    except RuntimeError as e:
        return {"ok": False, "model": config.OLLAMA_MODEL, "error": str(e)}


@app.get("/api/torrent-size")
async def torrent_size(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"size_gb": None}
    size = await asyncio.get_event_loop().run_in_executor(
        None, _dir_size if p.is_dir() else lambda _: p.stat().st_size, p
    )
    size_gb = round(size / 1e9, 1) if size else None
    if size_gb is not None:
        try:
            _db.set_size(p.name, size_gb)
        except Exception:
            pass
    return {"size_gb": size_gb}


@app.post("/api/browse")
async def browse() -> dict:
    try:
        r = subprocess.run(
            ["osascript", "-e", "POSIX path of (choose folder)"],
            capture_output=True, text=True, timeout=30,
        )
        path = r.stdout.strip()
        if path:
            return {"path": path}
        return {"error": "No folder selected"}
    except subprocess.TimeoutExpired:
        return {"error": "Picker timed out"}
    except Exception as e:
        return {"error": str(e)}


# ── REST: Inspect a movie (probe + AI name parse) ─────────────────────────────

@app.post("/api/inspect")
async def inspect(body: dict) -> dict:
    path = body.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)

    source = Path(path)
    if not source.exists():
        return JSONResponse({"error": f"Path does not exist: {path}"}, status_code=400)

    result = await asyncio.get_event_loop().run_in_executor(None, _do_inspect, source)
    return result


def _do_inspect(source: Path) -> dict:
    """Run ffprobe + AI name parse. Returns a dict suitable for the review panel."""
    video_file = _find_video(source)
    if video_file is None:
        return {"error": "No video file found in path"}

    # AI name parse
    try:
        movie_info = parse(source)
    except ContentSkipped as e:
        return {"error": f"Skipped: {e.reason}"}
    except Exception as e:
        return {"error": f"Name parse failed: {e}"}

    # ffprobe
    try:
        probe = analyze(video_file)
    except Exception as e:
        return {"error": f"Probe failed: {e}"}

    # Default audio selection (so UI shows sensible defaults)
    default_tracks = select_audio_tracks(probe)
    # Map codec_arg ("ac3"/"copy") to UI action names ("encode"/"copy")
    default_actions = {
        t.source_index: ("encode" if t.codec_arg == "ac3" else "copy")
        for t in default_tracks
    }
    # Tracks excluded by default (e.g. AAC covered by TrueHD)
    included_indices = {t.source_index for t in default_tracks}

    audio = []
    for a in probe.audio:
        action = default_actions.get(a.index, "copy")
        included = a.index in included_indices
        audio.append({
            "source_index": a.index,
            "codec":    a.codec,
            "channels": a.channels,
            "language": a.language,
            "title":    a.title,
            "bitrate":  a.bitrate,
            "profile":  a.profile,
            "action":   action if included else "exclude",
            "has_atmos": a.has_atmos,
        })

    subtitles = []
    for s in probe.subtitles:
        subtitles.append({
            "source_index": s.index,
            "codec":    s.codec,
            "language": s.language,
            "title":    s.title,
            "include":  True,
        })

    # Compute proposed output name
    res = movie_info.resolution or probe.resolution
    year_str = f" ({movie_info.year})" if movie_info.year else ""
    suffix = probe.folder_suffix
    output_name = f"{movie_info.title}{year_str} [{res}]{suffix}"

    base = f"{movie_info.title} ({movie_info.year})" if movie_info.year else movie_info.title
    existing_output = None
    if config.OUTPUT_DIR.exists():
        existing_output = next(
            (d.name for d in config.OUTPUT_DIR.iterdir()
             if d.is_dir() and d.name.startswith(base)),
            None,
        )

    return {
        "path":            str(source),
        "video_file":      str(video_file),
        "title":           movie_info.title,
        "year":            movie_info.year,
        "resolution":      res,
        "video": {
            "codec":     probe.video.codec,
            "width":     probe.video.width,
            "height":    probe.video.height,
            "fps":       probe.video.fps,
            "bit_depth": probe.video.bit_depth,
            "resolution": probe.resolution,
        },
        "audio":           audio,
        "subtitles":       subtitles,
        "output_name":     output_name,
        "existing_output": existing_output,
        "duration_secs":   probe.duration_secs,
        "size_bytes":      probe.size_bytes,
    }


# ── REST: Add confirmed job to encode queue ───────────────────────────────────

@app.post("/api/jobs")
async def add_job(body: dict) -> dict:
    path = body.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)

    job_id = str(uuid.uuid4())[:8]
    output_name = _compute_output_name(body)

    if not body.get("force", False) and config.OUTPUT_DIR.exists():
        title = body.get("title", "")
        year  = body.get("year")
        base  = f"{title} ({year})" if year else title
        existing = next(
            (d.name for d in config.OUTPUT_DIR.iterdir()
             if d.is_dir() and d.name.startswith(base)),
            None,
        )
        if existing:
            return JSONResponse(
                {"collision": True, "output_name": output_name, "existing": existing},
                status_code=409,
            )

    job = {
        "id":          job_id,
        "path":        path,
        "title":       body.get("title", Path(path).name),
        "year":        body.get("year"),
        "resolution":  body.get("resolution"),
        "audio":       body.get("audio", []),
        "subtitles":   body.get("subtitles", []),
        "output_name": output_name,
        "phase":       "queued",
        "stages": {
            "probe":    {"status": "waiting", "pct": 0, "label": ""},
            "crf_probe":{"status": "waiting", "pct": 0, "label": ""},
            "encode":   {"status": "waiting", "pct": 0, "label": "", "eta": ""},
        },
        "dry_run":       bool(body.get("dry_run", False)),
        "elapsed":       "",
        "eta":           "",
        "output_size_mb": 0,
        "error":         "",
        "start_time":    None,
    }

    with _jobs_lock:
        _jobs[job_id] = job

    with _encode_lock:
        _encode_queue.append(job_id)

    await _broadcast({"type": "job_added", "job": _job_view(job)})

    return {"job_id": job_id, "output_name": output_name}


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job["phase"] in ("encoding", "probing"):
            return JSONResponse({"error": "Cannot remove active job"}, status_code=409)
        _jobs.pop(job_id, None)

    with _encode_lock:
        if job_id in _encode_queue:
            _encode_queue.remove(job_id)

    await _broadcast({"type": "job_removed", "job_id": job_id})
    return {"ok": True}


@app.get("/api/jobs")
async def get_jobs() -> dict:
    with _jobs_lock:
        return {"jobs": [_job_view(j) for j in _jobs.values()]}


# ── REST: Queue control ───────────────────────────────────────────────────────

@app.post("/api/queue/start")
async def queue_start() -> dict:
    _maybe_start_worker()
    return {"running": _queue_running}


@app.post("/api/queue/stop")
async def queue_stop() -> dict:
    global _stop_requested
    _stop_requested = True
    return {"stopping": True}


@app.post("/api/queue/stop-current")
async def queue_stop_current() -> dict:
    killed = _kill_active_encode()
    return {"killed": killed}


# ── REST: Reveal in Finder ────────────────────────────────────────────────────

@app.post("/api/reveal/{job_id}")
async def reveal(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)

    output_name = job.get("output_name", "")
    output_file = config.OUTPUT_DIR / output_name / f"{output_name}.mkv"
    target = str(output_file) if output_file.exists() else str(config.OUTPUT_DIR)

    subprocess.run(["open", "-R", target])
    return {"ok": True}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    with _jobs_lock:
        current_jobs = [_job_view(j) for j in _jobs.values()]

    await ws.send_json({"type": "jobs_list", "jobs": current_jobs, "queue_running": _queue_running})

    with _ws_lock:
        _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            _ws_clients.discard(ws)


async def _broadcast(msg: dict) -> None:
    with _ws_lock:
        clients = list(_ws_clients)
    for ws in clients:
        try:
            await ws.send_json(msg)
        except Exception:
            with _ws_lock:
                _ws_clients.discard(ws)


def _broadcast_sync(msg: dict) -> None:
    """Call from worker thread to push a message to all WebSocket clients."""
    if _loop and not _loop.is_closed():
        future = asyncio.run_coroutine_threadsafe(_broadcast(msg), _loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass


# ── Encode worker ─────────────────────────────────────────────────────────────

def _maybe_start_worker() -> None:
    global _encode_thread, _queue_running
    if _queue_running:
        return
    with _encode_lock:
        if not _encode_queue:
            return
    _queue_running = True
    _encode_thread = threading.Thread(target=_worker_loop, daemon=True)
    _encode_thread.start()


def _worker_loop() -> None:
    global _queue_running, _stop_requested

    while True:
        with _encode_lock:
            if _stop_requested or not _encode_queue:
                _queue_running = False
                _stop_requested = False
                _broadcast_sync({"type": "queue_state", "running": False})
                return
            job_id = _encode_queue.pop(0)

        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            continue

        _run_job(job)


def _run_job(job: dict) -> None:
    job_id = job["id"]
    job["phase"] = "encoding"
    job["start_time"] = time.time()
    _set_stage(job, "probe", "active", 0, "Probing...")
    _broadcast_sync({"type": "job_update", "job": _job_view(job)})

    def on_progress(stage: str, pct: float, message: str = "", eta: str = "") -> None:
        if pct < 0:
            _set_stage(job, stage, "error", 0, message)
        elif pct >= 1.0:
            _set_stage(job, stage, "done", 100, message)
        else:
            _set_stage(job, stage, "active", int(pct * 100), message, eta)

        if job.get("start_time"):
            elapsed = time.time() - job["start_time"]
            job["elapsed"] = _fmt_time(elapsed)
        job["eta"] = eta

        _broadcast_sync({"type": "job_update", "job": _job_view(job)})

    user_overrides = {
        "title":     job.get("title"),
        "year":      job.get("year"),
        "resolution": job.get("resolution"),
        "audio":     job.get("audio") or None,
        "subtitles": job.get("subtitles") or None,
    }

    try:
        success = pipeline_run(
            Path(job["path"]),
            dry_run=job.get("dry_run", False),
            on_progress=on_progress,
            user_overrides=user_overrides,
        )
    except Exception as e:
        job["phase"] = "error"
        job["error"] = str(e)
        _broadcast_sync({"type": "job_update", "job": _job_view(job)})
        return

    if success:
        job["phase"] = "done"
        output_name = job.get("output_name", "")
        output_file = config.OUTPUT_DIR / output_name / f"{output_name}.mkv"
        if output_file.exists():
            job["output_size_mb"] = round(output_file.stat().st_size / 1e6)
    else:
        job["phase"] = "error"
        job["error"] = "Pipeline returned failure"

    _broadcast_sync({"type": "job_update", "job": _job_view(job)})


def _set_stage(job: dict, stage: str, status: str, pct: int, label: str, eta: str = "") -> None:
    if stage not in job["stages"]:
        return
    job["stages"][stage]["status"] = status
    job["stages"][stage]["pct"]    = pct
    job["stages"][stage]["label"]  = label
    if "eta" in job["stages"][stage]:
        job["stages"][stage]["eta"] = eta

    if status == "active":
        order = ["probe", "crf_probe", "encode"]
        for s in order:
            if s == stage:
                break
            if job["stages"].get(s, {}).get("status") in ("waiting", "active"):
                job["stages"][s]["status"] = "done"
                job["stages"][s]["pct"] = 100


# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_view(job: dict) -> dict:
    return {
        "id":            job["id"],
        "path":          job["path"],
        "title":         job.get("title", ""),
        "output_name":   job.get("output_name", ""),
        "phase":         job["phase"],
        "stages":        job["stages"],
        "elapsed":       job.get("elapsed", ""),
        "elapsed_secs":  round(time.time() - job["start_time"], 1) if job.get("start_time") else 0,
        "eta":           job.get("eta", ""),
        "output_size_mb": job.get("output_size_mb", 0),
        "error":         job.get("error", ""),
        "year":          job.get("year"),
        "resolution":    job.get("resolution"),
        "audio":         job.get("audio"),
        "subtitles":     job.get("subtitles"),
        "dry_run":       job.get("dry_run", False),
    }


def _compute_output_name(body: dict) -> str:
    title = body.get("title") or Path(body.get("path", "unknown")).name
    year = body.get("year")
    res  = body.get("resolution") or ""
    year_str = f" ({year})" if year else ""
    res_str  = f" [{res}]" if res else ""

    audio = body.get("audio") or []
    suffix = ""
    for a in audio:
        if a.get("action") == "exclude":
            continue
        codec = a.get("codec", "").lower()
        has_atmos = a.get("has_atmos", False)
        if codec in ("truehd", "mlp"):
            suffix = " TrueHD Atmos" if has_atmos else " TrueHD"
            break
        if codec == "eac3":
            suffix = " EAC3 Atmos" if has_atmos else " EAC3"
            break
        if codec in ("dts", "dca"):
            suffix = " DTS"
            break
        if codec == "ac3" or a.get("action") == "encode":
            suffix = " AC3"
            break

    return f"{title}{year_str}{res_str}{suffix}"


# ── AI classification ─────────────────────────────────────────────────────────

_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web to verify if a title is a movie, music album, TV show, band, software, etc. "
            "Use only when you genuinely cannot tell from the name alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'Blue All Rise 2001 movie or band album'",
                }
            },
            "required": ["query"],
        },
    },
}


def _web_search(query: str) -> str:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
        if not results:
            return "No results found."
        return "\n".join(f"- {r['title']}: {r['body'][:300]}" for r in results)
    except Exception as e:
        return f"Search unavailable: {e}"


def _classify_movies(names: list[str]) -> tuple[list[str], str]:
    """Two-phase AI classification:
    - Call 1: AI sorts all names into movies / not_movies / unsure (no tools, knowledge only)
    - Call 2: AI resolves only the 'unsure' items using web_search

    Returns (sorted movie names, thinking text).
    """
    import json
    import re
    import ollama

    if not names:
        return [], ""

    thinking_parts: list[str] = []

    # ── Call 1: confident pass (chunks of 50) ────────────────────────────────
    call1_prompt = (
        "You receive a JSON object mapping numeric keys to folder/file names from a torrent folder.\n"
        "Sort each key into exactly one of three buckets:\n"
        '  "movies"    — you are confident this is a movie or animated film\n'
        '  "not_movies" — you are confident this is NOT a movie (music, TV show, software, concert, book, etc.)\n'
        '  "unsure"    — you are not confident; it could go either way\n\n'
        "Be strict: only put something in movies or not_movies if you are genuinely confident.\n"
        "When in doubt, put it in unsure — a second pass will handle those.\n\n"
        "Russian/Cyrillic: песни/хиты/альбом/дискография = music (not_movies). "
        "Бременские музыканты / Простоквашино = animated films (movies).\n"
        "Video quality markers (1080p, BluRay, WEBRip, x265, HDRip) strongly indicate a movie.\n"
        "Movie box sets (e.g. 'Complete Collection', '1-6 Box Set') count as movies.\n\n"
        "Put in UNSURE if the name contains: [Vinyl Rip], Vinyl, OST, Soundtrack — "
        "even when the title is a known movie name, these usually mean the music release not the film.\n"
        "Put in UNSURE if the resolution is very low (576p, 480p) with no other quality markers.\n\n"
        'Return ONLY valid JSON: {"movies": [0, 3], "not_movies": [1, 2], "unsure": [4]}'
    )

    movies_from_call1: list[str] = []
    unsure: list[str] = []

    CHUNK_SIZE = 50
    chunks = [names[i:i + CHUNK_SIZE] for i in range(0, len(names), CHUNK_SIZE)]

    for chunk_idx, chunk in enumerate(chunks):
        print(f"[classify/call1] chunk {chunk_idx + 1}/{len(chunks)} ({len(chunk)} items)")
        indexed = {str(i): name for i, name in enumerate(chunk)}
        try:
            thinking_buf = ""
            reply_buf = ""
            for stream_chunk in ollama.Client().chat(
                model=config.OLLAMA_MODEL,
                think=True,
                options={"num_ctx": 16384},
                stream=True,
                messages=[
                    {"role": "system", "content": call1_prompt},
                    {"role": "user",   "content": json.dumps(indexed)},
                ],
            ):
                t = getattr(stream_chunk.message, "thinking", None) or ""
                if t:
                    thinking_buf += t
                    print(t, end="", flush=True)
                    _broadcast_sync({"type": "thinking_chunk", "text": t})
                c = stream_chunk.message.content or ""
                if c:
                    reply_buf += c

            if thinking_buf:
                print()
                thinking_parts.append(thinking_buf)
            reply = reply_buf.strip()

            reply = re.sub(r"^```[a-z]*\s*", "", reply)
            reply = re.sub(r"\s*```$", "", reply.strip())
            m = re.search(r"\{.*\}", reply, re.DOTALL)
            data = json.loads(m.group() if m else reply)

            movie_keys     = set(str(k) for k in data.get("movies", []))
            not_movie_keys = set(str(k) for k in data.get("not_movies", []))
            unsure_keys    = set(str(k) for k in data.get("unsure", []))

            # Any key the model dropped goes to unsure
            all_assigned = movie_keys | not_movie_keys | unsure_keys
            dropped_keys = {str(i) for i in range(len(chunk))} - all_assigned
            if dropped_keys:
                dropped_names = [chunk[int(k)] for k in sorted(dropped_keys, key=int)]
                print(f"[classify/call1] chunk {chunk_idx + 1} WARNING: {len(dropped_keys)} keys dropped → unsure: {dropped_names}")
                unsure_keys |= dropped_keys

            chunk_movies = [name for i, name in enumerate(chunk) if str(i) in movie_keys]
            chunk_unsure = [name for i, name in enumerate(chunk) if str(i) in unsure_keys]
            chunk_not    = [name for i, name in enumerate(chunk) if str(i) in not_movie_keys]

            print(f"[classify/call1] chunk {chunk_idx + 1}: movies={len(chunk_movies)} not_movies={len(chunk_not)} unsure={len(chunk_unsure)}")
            movies_from_call1.extend(chunk_movies)
            unsure.extend(chunk_unsure)

        except Exception as e:
            print(f"[classify/call1] chunk {chunk_idx + 1} error: {e} — treating all as movies")
            movies_from_call1.extend(chunk)

    print(f"[classify/call1] TOTAL: movies={len(movies_from_call1)} unsure={len(unsure)}")
    print(f"[classify/call1] MOVIES: {movies_from_call1}")
    print(f"[classify/call1] UNSURE: {unsure}")

    # ── Call 2: mandatory web search for every unsure item, then single AI call ─
    movies_from_call2: list[str] = []

    if unsure:
        print(f"[classify/call2] pre-searching {len(unsure)} unsure items...")
        search_data: dict[str, str] = {}
        for i, name in enumerate(unsure):
            query = name.replace('_', ' ')
            print(f"[search] ({i + 1}/{len(unsure)}) {query}")
            _broadcast_sync({"type": "ai_search", "query": f"({i + 1}/{len(unsure)}) {query}"})
            search_data[str(i)] = _web_search(query)

        user_parts = []
        for i, name in enumerate(unsure):
            results = search_data.get(str(i), "No results.")
            user_parts.append(f"[{i}] Name: {name}\nSearch results:\n{results}")
        user_content = "\n\n---\n\n".join(user_parts)

        call2_prompt = (
            "You receive a list of folder/file names together with web search results for each entry.\n"
            "Based on the search results and the name, classify each entry.\n"
            "Return ONLY a JSON array of keys (numbers) that are MOVIES.\n"
            "Example: [0, 2]\n\n"
            "INCLUDE: feature films, animated films (any country/genre), movie box sets (collections of multiple films).\n"
            "EXCLUDE: music albums, concerts, TV shows, software, books, games.\n\n"
            "Audio format clues — if these appear in the name, it is almost certainly music/audio, NOT a movie:\n"
            "  - [Vinyl Rip], Vinyl → music pressed on vinyl record\n"
            "  - .WAV, .CUE, Lossless, FLAC → lossless audio files, not video\n"
            "  - DTS.WAV, WAV.CUE → audio-only release\n"
            "Even if the title matches a known movie (e.g. 'Tron Legacy [Vinyl Rip]'), "
            "these audio markers mean it is the soundtrack or music release, not the film itself.\n\n"
            "Output ONLY the final JSON array."
        )

        messages2 = [
            {"role": "system", "content": call2_prompt},
            {"role": "user",   "content": user_content},
        ]

        reply2 = ""
        thinking_buf2 = ""
        try:
            for chunk in ollama.Client().chat(
                model=config.OLLAMA_MODEL,
                think=True,
                options={"num_ctx": 32768},
                stream=True,
                messages=messages2,
            ):
                t = getattr(chunk.message, "thinking", None) or ""
                if t:
                    thinking_buf2 += t
                    print(t, end="", flush=True)
                    _broadcast_sync({"type": "thinking_chunk", "text": t})
                c = chunk.message.content or ""
                if c:
                    reply2 += c

            if thinking_buf2:
                print()
                thinking_parts.append(thinking_buf2)

            reply2 = re.sub(r"^```[a-z]*\s*", "", reply2.strip())
            reply2 = re.sub(r"\s*```$", "", reply2.strip())
            m2 = re.search(r"\[.*\]", reply2, re.DOTALL)
            ai_keys2 = set(str(k) for k in json.loads(m2.group() if m2 else reply2))
            movies_from_call2 = [name for i, name in enumerate(unsure) if str(i) in ai_keys2]
            rejected2 = [name for i, name in enumerate(unsure) if str(i) not in ai_keys2]
            print(f"[classify/call2] movies={len(movies_from_call2)} rejected={len(rejected2)}")
            print(f"[classify/call2] MOVIES: {movies_from_call2}")
            print(f"[classify/call2] REJECTED: {rejected2}")

        except Exception as e:
            print(f"[classify/call2] error: {e}")
            movies_from_call2 = list(unsure)

    # Remove anything the AI miscounted as a movie but is clearly a TV episode
    _TV_EPISODE = _re.compile(r"\bS\d{1,2}E\d{1,2}\b|\(s\d{2}\)", _re.IGNORECASE)
    tv_filtered = [n for n in movies_from_call1 + movies_from_call2 if _TV_EPISODE.search(n)]
    all_movies  = [n for n in movies_from_call1 + movies_from_call2 if not _TV_EPISODE.search(n)]
    if tv_filtered:
        print(f"[classify] TV filter removed: {tv_filtered}")
    print(f"[classify] TOTAL MOVIES ({len(all_movies)}): {all_movies}")
    thinking = "\n\n".join(thinking_parts)
    all_movies.sort(key=lambda n: re.sub(r"^(the|a|an)\s+", "", n, flags=re.IGNORECASE).lower())
    return all_movies, thinking


def _classify_others(names: list[str]) -> dict[str, str]:
    """Ask AI to categorize non-movie items. Returns {name: category}."""
    import json
    import re
    import ollama

    indexed = {str(i): name for i, name in enumerate(names)}
    try:
        response = ollama.Client().chat(
            model=config.OLLAMA_MODEL,
            think=False,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You receive a JSON object mapping numeric keys to folder/file names.\n"
                        "Return ONLY valid JSON mapping each key to its category.\n"
                        'Example: {"0": "music", "1": "tv", "2": "software", "3": "other"}\n'
                        "Categories:\n"
                        "  tv       — TV series, episodes, seasons\n"
                        "  concert  — live music performance video\n"
                        "  music    — music albums, artist discographies, audio files, vinyl rips, soundtracks\n"
                        "  software — applications, games, tools\n"
                        "  other    — books, documents, or anything else"
                    ),
                },
                {"role": "user", "content": json.dumps(indexed)},
            ],
        )
        reply = (response.message.content or "").strip()
        reply = re.sub(r"^```[a-z]*\s*", "", reply)
        reply = re.sub(r"\s*```$", "", reply.strip())
        m = re.search(r"\{.*\}", reply, re.DOTALL)
        data = json.loads(m.group() if m else reply)
        valid = {"tv", "concert", "music", "software", "other"}
        return {name: (data.get(str(i), "other") if data.get(str(i)) in valid else "other")
                for i, name in enumerate(names)}
    except Exception:
        return {n: _categorize(n) for n in names}


def _categorize(name: str) -> str:
    """Classify a folder/file name using the same skip patterns as the pipeline."""
    try:
        _check_skip(name)
        return "movie"
    except ContentSkipped as e:
        reason = e.reason.lower()
        if "tv" in reason or "season" in reason or "episode" in reason:
            return "tv"
        if "concert" in reason:
            return "concert"
        return "other"


import re as _re

_WEB_MUSIC_EXTRA = _re.compile(
    r"\b(kbps|lossless|vinyl|discography|LP|EP)\b"
    r"|\baudio[\s_]+dvd\b"
    r"|__(?:aac|mp3|flac|wav)$",
    _re.IGNORECASE,
)
_WEB_DOCS_EXTRA = _re.compile(
    r"\[chm\]|\.(chm|pdf|epub|djvu)\b"
    r"|\b(?:training[\s_]+documents?|technical[\s_]+training|workshop[\s_]+manual|teach\s+yourself)\b",
    _re.IGNORECASE,
)
_WEB_CONCERT_EXTRA = _re.compile(
    r"\blive[\s_]+(?:at|in|tour)\b",
    _re.IGNORECASE,
)
_VIDEO_QUALITY = _re.compile(
    r"\b(?:1080p|720p|2160p|480p|4k|uhd|bluray|blu.ray|webrip|web.dl|hdtv|dvdrip|hevc|h265|h264|x265|x264|remux)\b",
    _re.IGNORECASE,
)
_ARTIST_ALBUM  = _re.compile(r"^[^.\[\]<>]+\s+-\s+[^.\[\]<>]+$")
_ENDS_WITH_YEAR = _re.compile(r"\(\d{4}\)\s*$")


def _web_categorize(name: str) -> str:
    """Extended categorization for the web UI — catches music/docs the pipeline regex misses."""
    norm = name.replace("_", " ")

    base = _categorize(name)
    if base != "movie":
        return base

    if _WEB_MUSIC_EXTRA.search(norm):
        return "other"
    if _WEB_DOCS_EXTRA.search(norm):
        return "other"
    if _WEB_CONCERT_EXTRA.search(norm):
        return "concert"

    if (_ARTIST_ALBUM.match(norm)
            and not _VIDEO_QUALITY.search(norm)
            and not _ENDS_WITH_YEAR.search(norm)):
        return "other"

    return "movie"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def _fmt_time(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ── Entry point ───────────────────────────────────────────────────────────────

def start(host: str = "127.0.0.1", port: int = 8081) -> None:
    import uvicorn
    url = f"http://{host}:{port}"
    print(f"Movie Standardizer web UI → {url}")

    def _open():
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
