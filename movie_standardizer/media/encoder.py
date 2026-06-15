"""AV1 video encoder with CRF probe tuning.

Two-stage process:
  1. Probe: extract 10 x 1-min video-only clips spread across the film,
     encode each to AV1 at the candidate CRF, compare sizes to source.
     Raise CRF until the sample encodes to <= PROBE_TARGET_RATIO of source.
  2. Full encode: one ffmpeg pass -- video AV1 at chosen CRF, audio per
     the AudioOutputTrack list (AC3 encode or copy), all subtitles copied.

Output is written atomically via a .tmp.mkv intermediate that is renamed
to the final path only on success.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from .audio import AudioOutputTrack, build_audio_args
from .streams import ProbeResult
from .. import config


# ── Public entry point ────────────────────────────────────────────────────────

def encode(
    result:        ProbeResult,
    audio_tracks:  list[AudioOutputTrack],
    output_path:   Path,
    dry_run:       bool = False,
) -> bool:
    """Encode result.path -> output_path.

    Returns True on success, False on failure.
    The output directory is created if it does not exist.
    """
    input_path = result.path
    tmp_path   = output_path.with_suffix(".tmp.mkv")

    source_codec   = result.video.codec
    input_size     = result.size_bytes
    duration_secs  = result.duration_secs

    initial_crf = _crf_for_source(source_codec, input_size, duration_secs)

    if dry_run:
        cmd = _build_command(input_path, tmp_path, audio_tracks, result, initial_crf)
        print(f"  [dry-run] CRF={initial_crf} codec={source_codec}")
        print("  " + " ".join(_quote(c) for c in cmd))
        return True

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if tmp_path.exists():
        tmp_path.unlink()

    log_path = _log_path(input_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    input_mb  = input_size / (1024 * 1024)
    dur_min   = int(duration_secs // 60)
    bitrate   = int((input_size * 8) / (duration_secs * 1000)) if duration_secs > 0 else 0
    print(
        f"  Source: {source_codec}  {result.resolution}  "
        f"{bitrate} kbps  {input_mb:.0f} MB  {dur_min}min  "
        f"initial CRF={initial_crf}",
        flush=True,
    )

    # Probe-encode to tune CRF (only for files longer than 20 min)
    if duration_secs > 1200:
        crf = _probe_crf(input_path, source_codec, input_size, duration_secs,
                         initial_crf, log_path)
    else:
        crf = initial_crf

    cmd = _build_command(input_path, tmp_path, audio_tracks, result, crf)

    print(f"  Encoding  CRF={crf}", flush=True)
    with open(log_path, "a", encoding="utf-8") as log_fh:
        log_fh.write(f"\n# Full encode -- CRF {crf}\n")
        log_fh.write("# " + " ".join(_quote(c) for c in cmd) + "\n\n")
        log_fh.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=log_fh,
            text=True,
        )
        _run_with_progress(proc, duration_secs)

    if proc.returncode != 0:
        _cleanup(tmp_path)
        print(f"  ERROR: ffmpeg exited {proc.returncode} -- see {log_path}", flush=True)
        return False

    print(f"\r{' ' * 80}\r", end="", flush=True)
    tmp_path.rename(output_path)
    out_mb = output_path.stat().st_size / (1024 * 1024)
    ratio  = output_path.stat().st_size / input_size if input_size > 0 else 0
    print(f"  Done: {out_mb:.0f} MB  ratio={ratio:.2f}  saved {input_mb - out_mb:.0f} MB", flush=True)
    return True


# ── Command builder ───────────────────────────────────────────────────────────

def _build_command(
    input_path:   Path,
    output_path:  Path,
    audio_tracks: list[AudioOutputTrack],
    result:       ProbeResult,
    crf:          int,
) -> list[str]:
    cmd = [
        config.FFMPEG_BIN,
        "-hide_banner", "-loglevel", "warning",
        "-progress", "pipe:1", "-nostats",
        "-i", str(input_path),
    ]

    # Map primary video stream explicitly (skips cover art)
    cmd += ["-map", f"0:v:{0}"]

    # Audio maps and codec args from audio processor
    cmd += build_audio_args(audio_tracks)

    # Subtitles -- map all, copy or transcode as needed
    if result.subtitles:
        cmd += ["-map", "0:s"]
        sub_codec = _subtitle_codec(result)
        cmd += ["-c:s", sub_codec]

    # Video encoding
    cmd += [
        "-c:v",         config.VIDEO_CODEC,
        "-crf",         str(crf),
        "-preset",      str(config.PRESET),
        "-svtav1-params", config.SVTAV1_PARAMS,
    ]

    cmd += [
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-y",
        str(output_path),
    ]

    return cmd


def _subtitle_codec(result: ProbeResult) -> str:
    for s in result.subtitles:
        if s.codec.lower() in config.SUB_TRANSCODE:
            return config.SUB_TRANSCODE[s.codec.lower()]
    return "copy"


# ── CRF selection ─────────────────────────────────────────────────────────────

def _crf_for_source(codec: str, input_size: int, duration_secs: float) -> int:
    if duration_secs > 0:
        bitrate_kbps = (input_size * 8) / (duration_secs * 1000)
    else:
        bitrate_kbps = 4000

    base_crf = config.CRF_BITRATE_MAX
    for threshold, crf in config.CRF_BITRATE_TIERS:
        if bitrate_kbps < threshold:
            base_crf = crf
            break

    offset = config.CRF_CODEC_OFFSET.get(codec.lower(), 0)
    return max(config.CRF_MIN, min(base_crf + offset, config.CRF_MAX))


# ── CRF probe ────────────────────────────────────────────────────────────────

def _probe_crf(
    input_path:    Path,
    source_codec:  str,
    input_size:    int,
    duration_secs: float,
    initial_crf:   int,
    log_path:      Path,
) -> int:
    """Encode 10 x 1-min video-only clips to find the best CRF.

    Raises CRF until the sample encodes to <= PROBE_TARGET_RATIO of source.
    Returns the chosen CRF. Cleans up all temp files on exit.
    """
    positions   = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95]
    clip_dur    = 60
    clip_starts = [int(duration_secs * p) for p in positions]
    pct_labels  = [f"{int(p * 100)}%" for p in positions]

    stem  = input_path.stem[:40]
    tmpd  = Path("/tmp")
    clips = [tmpd / f"_probe_clip{i}_{stem}.ts"  for i in range(len(positions))]
    encs  = [tmpd / f"_probe_enc{i}_{stem}.mkv"  for i in range(len(positions))]

    crf        = initial_crf
    chosen_crf = initial_crf

    def _status(msg: str) -> None:
        print(f"  {msg}", flush=True)

    try:
        _status(f"Probe: extracting 10 x 1-min clips ({', '.join(pct_labels)})")

        for start, clip in zip(clip_starts, clips):
            try:
                r = subprocess.run([
                    config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
                    "-fflags", "+genpts",
                    "-ss", str(start), "-t", str(clip_dur),
                    "-i", str(input_path),
                    "-map", "0:v", "-c:v", "copy",
                    "-y", str(clip),
                ], capture_output=True, timeout=300)
            except subprocess.TimeoutExpired:
                _status("Probe: clip extraction timed out -- using initial CRF")
                return crf
            if r.returncode != 0 or not clip.exists():
                _status("Probe: clip extraction failed -- using initial CRF")
                return crf

        sample_source_size = sum(c.stat().st_size for c in clips if c.exists())
        _status("Probe: clips ready (10 x 1-min)")

        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n# Probe: positions={pct_labels}, clip_dur={clip_dur}s\n")

        for attempt in range(8):
            _status(f"Probe: attempt {attempt + 1}/8  CRF={crf}")

            total_enc_size = 0
            ok = True
            for label, clip, enc in zip(pct_labels, clips, encs):
                _status(f"Probe: encoding clip {label}  CRF={crf}")
                enc_cmd = [
                    config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
                    "-progress", "pipe:1", "-nostats",
                    "-i", str(clip),
                    "-map", "0:v",
                    "-c:v", config.VIDEO_CODEC,
                    "-crf", str(crf),
                    "-preset", str(config.PRESET),
                    "-svtav1-params", config.SVTAV1_PARAMS,
                    "-y", str(enc),
                ]
                with open(log_path, "a", encoding="utf-8") as lf:
                    proc = subprocess.Popen(
                        enc_cmd,
                        stdout=subprocess.PIPE,
                        stderr=lf,
                        text=True,
                    )
                _run_with_progress(proc, clip_dur)
                print()
                if proc.returncode != 0 or not enc.exists():
                    ok = False
                    break
                total_enc_size += enc.stat().st_size

            if not ok:
                _status("Probe: encode failed -- using current CRF")
                break

            ratio   = total_enc_size / sample_source_size if sample_source_size > 0 else 1.0
            src_mb  = sample_source_size / (1024 * 1024)
            enc_mb  = total_enc_size / (1024 * 1024)
            target  = int(config.PROBE_TARGET_RATIO * 100)

            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(
                    f"# Probe attempt {attempt + 1}: CRF={crf}  "
                    f"ratio={ratio:.3f}  enc={total_enc_size // 1024}KB  "
                    f"src~={sample_source_size // 1024}KB\n"
                )

            if ratio <= config.PROBE_TARGET_RATIO:
                _status(
                    f"Probe: OK  ratio={ratio:.2f}  "
                    f"({enc_mb:.0f}MB vs ~{src_mb:.0f}MB)  CRF={crf} accepted"
                )
                chosen_crf = crf
                break

            delta      = 6.0 * math.log2(ratio / config.PROBE_TARGET_RATIO)
            delta      = max(1, round(delta))
            next_crf   = min(crf + delta, config.CRF_MAX)
            _status(
                f"Probe: too large  ratio={ratio:.2f}  "
                f"({enc_mb:.0f}MB vs ~{src_mb:.0f}MB)  "
                f"need <{target}%  -> raising CRF {crf} -> {next_crf} (+{delta})"
            )
            chosen_crf = next_crf
            crf        = next_crf

            for p in encs:
                try:
                    p.unlink()
                except OSError:
                    pass

            if crf >= config.CRF_MAX:
                _status(f"Probe: reached CRF_MAX ({config.CRF_MAX}) -- proceeding")
                break

        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"# Probe selected CRF={chosen_crf}\n")

        return chosen_crf

    finally:
        for p in clips + encs:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


# ── Progress bar ──────────────────────────────────────────────────────────────

def _run_with_progress(proc: subprocess.Popen, duration_secs: float) -> None:
    fields: dict[str, str] = {}
    try:
        for line in proc.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            fields[key] = val
            if key == "progress":
                _print_bar(fields, duration_secs)
                fields = {}
    except Exception:
        proc.kill()
        proc.wait()
        raise
    proc.wait()


def _print_bar(fields: dict, duration_secs: float) -> None:
    try:
        elapsed = int(fields.get("out_time_us", 0) or 0) / 1_000_000
    except (ValueError, TypeError):
        elapsed = 0

    pct       = min(elapsed / duration_secs, 1.0) if duration_secs > 0 else 0.0
    bar_width = 28
    bar       = "#" * int(bar_width * pct) + "-" * (bar_width - int(bar_width * pct))

    try:
        speed = float((fields.get("speed") or "0").replace("x", ""))
    except (ValueError, TypeError):
        speed = 0.0

    if speed > 0 and duration_secs > 0:
        remaining = (duration_secs - elapsed) / speed
        eta = _fmt_time(remaining)
    else:
        eta = "--:--"

    fps         = fields.get("fps", "?") or "?"
    speed_label = f"{speed:.1f}x" if speed > 0 else "?x"

    line = f"  [{bar}] {pct * 100:5.1f}%  ETA {eta}  {speed_label}  {fps} fps"
    print(f"\r{line:<72}", end="", flush=True)


def _fmt_time(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m:02d}m{s:02d}s"


# ── Utilities ─────────────────────────────────────────────────────────────────

def _log_path(input_path: Path) -> Path:
    safe = input_path.name.replace("/", "__")
    return config.LOGS_DIR / (safe + ".log")


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _quote(s: str) -> str:
    import re
    if re.search(r"[\s\"'\\]", s):
        return f'"{s}"'
    return s
