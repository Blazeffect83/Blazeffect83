#!/usr/bin/env python3
"""video_editor.py — Video Editing Assistant

Analyzes a folder of video clips with ffprobe, interprets a natural-language
edit request, generates an FFmpeg-based edit plan (saved as JSON), previews
it, then executes it on confirmation.

Usage:
    python video_editor.py ./clips/
    python video_editor.py ./clips/ --output final.mp4 --plan my_edit.json
    python video_editor.py ./clips/ --load-plan            # re-run saved plan
    python video_editor.py ./clips/ --no-execute           # preview only

Requirements: ffmpeg and ffprobe must be on PATH. No extra Python packages.
"""

import os
import sys
import json
import subprocess
import argparse
import re
import shutil
from pathlib import Path
from fractions import Fraction

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac"}

# ---------------------------------------------------------------------------
# Low-level FFprobe / FFmpeg helpers
# ---------------------------------------------------------------------------


def ffprobe_json(path: str, *extra_args: str) -> dict | None:
    """Run ffprobe on *path* and return parsed JSON, or None on failure."""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", *extra_args, str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def ffmpeg_run(args: list[str], verbose: bool = False) -> subprocess.CompletedProcess:
    """Run ffmpeg with *args*; optionally print the command."""
    cmd = ["ffmpeg", "-hide_banner", *args]
    if verbose:
        print("    $", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def parse_time(s: str) -> float:
    """Parse '1:30.5', '90', '1:02:03' etc. into seconds."""
    parts = s.strip().split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        else:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return 0.0


def fmt_time(seconds: float) -> str:
    """Format seconds as M:SS.mmm (or H:MM:SS.mmm for >= 1 hour)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"


# ---------------------------------------------------------------------------
# Clip analysis
# ---------------------------------------------------------------------------


def analyze_clip(path: str) -> dict:
    """
    Extract metadata from one video clip via ffprobe + volumedetect.

    Returns a dict with keys:
        path, filename, duration, size_bytes,
        width, height, fps, video_codec, pix_fmt,
        audio_codec, sample_rate, channels,
        audio_mean_volume_db, audio_max_volume_db
    """
    data = ffprobe_json(path, "-show_streams", "-show_format")
    if data is None:
        return {"path": path, "filename": Path(path).name, "error": "ffprobe failed"}

    info: dict = {"path": str(path), "filename": Path(path).name}

    fmt = data.get("format", {})
    info["duration"] = float(fmt.get("duration") or 0)
    info["size_bytes"] = int(fmt.get("size") or 0)

    for stream in data.get("streams", []):
        ct = stream.get("codec_type")
        if ct == "video" and "width" not in info:
            try:
                fps = float(Fraction(stream.get("r_frame_rate", "0/1")))
            except (ValueError, ZeroDivisionError):
                fps = 0.0
            info.update(
                {
                    "width": stream.get("width", 0),
                    "height": stream.get("height", 0),
                    "fps": round(fps, 3),
                    "video_codec": stream.get("codec_name", "unknown"),
                    "pix_fmt": stream.get("pix_fmt", "unknown"),
                }
            )
        elif ct == "audio" and "audio_codec" not in info:
            info.update(
                {
                    "audio_codec": stream.get("codec_name", "unknown"),
                    "sample_rate": int(stream.get("sample_rate") or 0),
                    "channels": int(stream.get("channels") or 0),
                }
            )

    # Audio levels via volumedetect filter
    r = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-vn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "/dev/null",
        ],
        capture_output=True,
        text=True,
    )
    for pattern, key in [
        (r"mean_volume:\s*([-\d.]+)\s*dB", "audio_mean_volume_db"),
        (r"max_volume:\s*([-\d.]+)\s*dB", "audio_max_volume_db"),
    ]:
        m = re.search(pattern, r.stderr)
        info[key] = float(m.group(1)) if m else None

    return info


def analyze_folder(folder: str) -> list[dict]:
    """Analyze every video file in *folder* and print a summary table."""
    p = Path(folder)
    files = sorted(f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS)
    if not files:
        print(f"  No video files found in {folder}")
        return []

    print(f"\n  Analyzing {len(files)} clip(s)...\n")
    clips: list[dict] = []
    for i, f in enumerate(files, 1):
        print(f"  [{i:2}/{len(files)}] {f.name:<44}", end="", flush=True)
        info = analyze_clip(str(f))
        clips.append(info)
        if "error" not in info:
            w = info.get("width", "?")
            h = info.get("height", "?")
            dur = info.get("duration", 0)
            vol = info.get("audio_mean_volume_db")
            vol_s = f"{vol:+.1f} dBFS" if vol is not None else "no audio"
            print(f"  {dur:6.1f}s  {w}x{h}  {vol_s}")
        else:
            print("  ERROR — skipped")

    return clips


# ---------------------------------------------------------------------------
# Silence detection
# ---------------------------------------------------------------------------


def detect_silences(
    path: str, noise_db: float = -35.0, min_dur: float = 0.5
) -> list[dict]:
    """
    Return a list of {start, end} silence intervals for one clip.

    Uses ffmpeg's silencedetect filter.
    """
    r = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-vn",
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_dur}",
            "-f",
            "null",
            "/dev/null",
        ],
        capture_output=True,
        text=True,
    )
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
    return [{"start": s, "end": e} for s, e in zip(starts, ends)]


def invert_silences(
    silences: list[dict], total_dur: float, pad: float = 0.05
) -> list[dict]:
    """
    Convert a list of silence intervals into non-silent segments to keep.

    *pad* seconds of audio are trimmed around each silence boundary so that
    plosives / breath sounds at the edges are removed cleanly.
    """
    segs: list[dict] = []
    cursor = 0.0
    for s in silences:
        seg_end = s["start"] - pad
        if seg_end > cursor + 0.05:
            segs.append({"start": round(cursor, 4), "end": round(seg_end, 4)})
        cursor = s["end"] + pad
    if cursor < total_dur - 0.05:
        segs.append({"start": round(cursor, 4), "end": round(total_dur, 4)})
    return segs


# ---------------------------------------------------------------------------
# Natural-language edit request parser
# ---------------------------------------------------------------------------


def _extract_duration_secs(text: str) -> float | None:
    """Pull a duration out of text like '60 seconds', '2 minutes', '90s'."""
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(second|sec|minute|min|s|m)\b", text, re.IGNORECASE
    )
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("m") and unit != "ms":
        val *= 60
    return val


def parse_edit_request(request: str, clips: list[dict]) -> dict:
    """
    Map a natural-language edit request to a structured plan dict.

    Recognised patterns
    -------------------
    cut_silences   "cut out silences", "remove quiet parts", "strip dead air"
    highlight_reel "60 second highlight reel", "2 minute montage"
    beat_sync      "sync cuts to the beat at 128 bpm", "cut to 'music.mp3'"
    trim           "trim from 0:30 to 2:45", "cut from 10s to 50s"
    concatenate    "join all clips", "stitch together", or no pattern matched
    """
    rl = request.lower()
    valid = [c for c in clips if "error" not in c]
    total = sum(c.get("duration", 0) for c in valid)

    plan: dict = {
        "request": request,
        "type": "unknown",
        "params": {},
        "operations": [],
    }

    # ── 1. Cut silences ──────────────────────────────────────────────────────
    if re.search(r"\b(silence|silent|quiet|dead.?air|mute)\b", rl):
        db_m = re.search(r"(-?\d+(?:\.\d+)?)\s*db", rl)
        dur_m = re.search(r"(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b", rl)
        noise = float(db_m.group(1)) if db_m else -35.0
        minsil = float(dur_m.group(1)) if dur_m else 0.5
        plan.update(
            {
                "type": "cut_silences",
                "params": {
                    "noise_floor_db": noise,
                    "min_silence_duration": minsil,
                },
                "operations": [
                    {
                        "clip": c["path"],
                        "action": "cut_silences",
                        "noise_floor_db": noise,
                        "min_silence_duration": minsil,
                    }
                    for c in valid
                ],
            }
        )

    # ── 2. Highlight reel / montage ──────────────────────────────────────────
    elif re.search(r"\b(highlight|reel|montage|compilation|summary)\b", rl):
        target = _extract_duration_secs(rl) or 60.0
        ratio = min(target / total, 1.0) if total > 0 else 1.0
        ops = []
        for c in valid:
            dur = c.get("duration", 0)
            take = dur * ratio
            mid = (dur - take) / 2  # centre-crop each clip
            ops.append(
                {
                    "clip": c["path"],
                    "action": "trim",
                    "start": round(mid, 3),
                    "end": round(mid + take, 3),
                }
            )
        plan.update(
            {
                "type": "highlight_reel",
                "params": {"target_duration": target},
                "operations": ops,
            }
        )

    # ── 3. Beat sync ─────────────────────────────────────────────────────────
    elif re.search(r"\b(beat|beats|bpm|sync|rhythm|music)\b", rl):
        bpm_m = re.search(r"(\d+(?:\.\d+)?)\s*bpm", rl)
        bpm = float(bpm_m.group(1)) if bpm_m else 120.0
        # Look for a referenced audio file (quoted or bare extension)
        af_m = re.search(
            r"""[\"']?([^\s\"']+\.(?:mp3|wav|aac|m4a|ogg|flac))[\"']?""",
            request,
            re.IGNORECASE,
        )
        audio = af_m.group(1) if af_m else None
        beat_s = round(60.0 / bpm, 4)
        ops = []
        for c in valid:
            take = min(beat_s * 4, c.get("duration", beat_s * 4))  # 4 beats per clip
            ops.append(
                {
                    "clip": c["path"],
                    "action": "trim",
                    "start": 0.0,
                    "end": round(take, 3),
                }
            )
        plan.update(
            {
                "type": "beat_sync",
                "params": {"bpm": bpm, "beat_interval_s": beat_s, "audio_file": audio},
                "operations": ops,
            }
        )

    # ── 4. Explicit trim ─────────────────────────────────────────────────────
    elif re.search(r"\b(trim|cut|clip|from|between)\b", rl):
        # Grab up to two time values from the request
        raw_times = re.findall(
            r"(\d+(?::\d{1,2}(?:\.\d+)?)?(?:\.\d+)?)\s*s?\b", rl
        )
        times = [parse_time(t) for t in raw_times[:2]]
        start = times[0] if times else 0.0
        end = times[1] if len(times) >= 2 else None
        ops = []
        for c in valid:
            dur = c.get("duration", 0)
            ops.append(
                {
                    "clip": c["path"],
                    "action": "trim",
                    "start": start,
                    "end": min(end, dur) if end is not None else dur,
                }
            )
        plan.update(
            {
                "type": "trim",
                "params": {"start": start, "end": end},
                "operations": ops,
            }
        )

    # ── 5. Concatenate (catch-all) ────────────────────────────────────────────
    else:
        plan.update(
            {
                "type": "concatenate",
                "params": {},
                "operations": [
                    {
                        "clip": c["path"],
                        "action": "include",
                        "start": 0.0,
                        "end": c.get("duration", 0),
                    }
                    for c in valid
                ],
            }
        )

    return plan


# ---------------------------------------------------------------------------
# Edit plan preview
# ---------------------------------------------------------------------------

_W = 66  # box width (inner)


def _box_line(text: str = "") -> str:
    return f"  | {text:<{_W}} |"


def preview_edit_plan(plan: dict) -> None:
    """Print the edit plan as a formatted table."""
    border = "  +" + "-" * (_W + 2) + "+"
    print()
    print(border)
    print(_box_line("  EDIT PLAN PREVIEW"))
    print(border)
    print(_box_line(f"  Type    : {plan['type'].replace('_', ' ').title()}"))
    req = plan["request"]
    print(_box_line(f"  Request : {req[:_W - 12]}"))

    params = {k: v for k, v in plan.get("params", {}).items() if v is not None}
    if params:
        param_str = "  ".join(f"{k}={v}" for k, v in params.items())
        print(_box_line(f"  Params  : {param_str[:_W - 12]}"))

    ops = plan.get("operations", [])
    print(border)
    print(_box_line(f"  {len(ops)} operation(s):"))
    print(_box_line())

    total_out = 0.0
    for i, op in enumerate(ops, 1):
        name = Path(op["clip"]).name
        action = op.get("action", "?")
        start = op.get("start", 0.0) or 0.0
        end = op.get("end", 0.0) or 0.0
        dur = end - start if end is not None else 0.0
        total_out += dur

        if action == "cut_silences":
            detail = (
                f"remove silences  "
                f"noise={op.get('noise_floor_db', -35)}dB  "
                f"min={op.get('min_silence_duration', 0.5)}s"
            )
        else:
            detail = f"{fmt_time(start)} -> {fmt_time(end)}  ({dur:.2f}s)"

        line = f"  {i:2}. {name}"
        print(_box_line(line))
        print(_box_line(f"       {detail}"))
        print(_box_line())

    print(border)
    if plan["type"] != "cut_silences":
        print(_box_line(f"  Estimated output duration: ~{total_out:.1f}s  ({fmt_time(total_out)})"))
    print(border)


# ---------------------------------------------------------------------------
# Plan execution helpers
# ---------------------------------------------------------------------------


def _encode_segment(src: str, start: float, end: float, out: str) -> bool:
    """Trim and re-encode a segment to H.264/AAC."""
    r = ffmpeg_run(
        [
            "-y",
            "-ss", str(start),
            "-to", str(end),
            "-i", src,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            out,
        ]
    )
    if r.returncode != 0:
        print(f"      ffmpeg error: {r.stderr[-300:]}")
    return r.returncode == 0


def _concat_clips(paths: list[str], out: str, tmp_dir: str) -> bool:
    """Concatenate pre-encoded clips via the concat demuxer."""
    if not paths:
        return False
    if len(paths) == 1:
        r = ffmpeg_run(["-y", "-i", paths[0], "-c", "copy", out])
        return r.returncode == 0

    list_file = os.path.join(tmp_dir, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    r = ffmpeg_run(
        ["-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out]
    )
    if r.returncode != 0:
        print(f"      concat error: {r.stderr[-300:]}")
    return r.returncode == 0


def _process_cut_silences(op: dict, tmp_dir: str, idx: int) -> str | None:
    """
    Detect silences in one clip and return a path to the silence-removed clip.
    Returns the original path if no silences are found, None on failure.
    """
    path = op["clip"]
    noise = op.get("noise_floor_db", -35.0)
    minsil = op.get("min_silence_duration", 0.5)

    print(f"    Detecting silences (noise={noise}dB, min={minsil}s)...")
    silences = detect_silences(path, noise, minsil)
    print(f"    Found {len(silences)} silence interval(s).")

    if not silences:
        print("    No silences — keeping clip as-is.")
        return path

    data = ffprobe_json(path, "-show_format")
    dur = float(((data or {}).get("format") or {}).get("duration") or 0)
    segs = invert_silences(silences, dur)

    if not segs:
        print("    Warning: entire clip is silent — skipping.")
        return None

    print(f"    Keeping {len(segs)} non-silent segment(s)...")
    tmp_segs: list[str] = []
    for j, seg in enumerate(segs):
        seg_out = os.path.join(tmp_dir, f"clip{idx:02d}_seg{j:03d}.mp4")
        if _encode_segment(path, seg["start"], seg["end"], seg_out):
            tmp_segs.append(seg_out)

    if not tmp_segs:
        return None

    merged = os.path.join(tmp_dir, f"clip{idx:02d}_nosil.mp4")
    ok = _concat_clips(tmp_segs, merged, tmp_dir)
    return merged if ok else None


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------


def execute_edit_plan(plan: dict, output_path: str) -> bool:
    """
    Run all operations defined in *plan* and write the result to *output_path*.

    Temporary files are placed in a _vedit_tmp/ subdirectory next to the
    output file and cleaned up automatically on success or failure.
    """
    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    tmp_dir = os.path.join(out_dir, "_vedit_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    ops = plan.get("operations", [])
    edit_type = plan.get("type", "concatenate")
    processed: list[str] = []

    print()
    print("  " + "=" * 60)
    print("  Executing edit plan...")
    print("  " + "=" * 60)

    try:
        for i, op in enumerate(ops):
            name = Path(op["clip"]).name
            print(f"\n  [{i + 1}/{len(ops)}] {name}")

            action = op.get("action", "include")

            if action == "cut_silences":
                result = _process_cut_silences(op, tmp_dir, i)
                if result:
                    processed.append(result)

            elif action in ("trim", "trim_to_beat"):
                start = float(op.get("start") or 0.0)
                end = float(op.get("end") or 0.0)
                print(f"    Trim  {fmt_time(start)} -> {fmt_time(end)}  ({end - start:.2f}s)")
                seg_out = os.path.join(tmp_dir, f"clip{i:02d}.mp4")
                if _encode_segment(op["clip"], start, end, seg_out):
                    processed.append(seg_out)
                else:
                    print(f"    WARNING: encoding failed — skipping {name}")

            elif action == "include":
                # Passthrough — re-encode to a common format so concat works
                start = float(op.get("start") or 0.0)
                end = float(op.get("end") or 0.0)
                seg_out = os.path.join(tmp_dir, f"clip{i:02d}.mp4")
                if _encode_segment(op["clip"], start, end, seg_out):
                    processed.append(seg_out)
                else:
                    print(f"    WARNING: encoding failed — skipping {name}")

            else:
                # Unknown action — pass the clip through unmodified
                processed.append(op["clip"])

        if not processed:
            print("\n  ERROR: No clips survived processing.")
            return False

        # ── Concatenate all processed clips ──────────────────────────────────
        audio_file = plan.get("params", {}).get("audio_file")

        print(f"\n  Concatenating {len(processed)} clip(s)...")
        pre_final = os.path.join(tmp_dir, "pre_final.mp4")
        if not _concat_clips(processed, pre_final, tmp_dir):
            print("  ERROR: Concatenation failed.")
            return False

        # ── Overlay external audio for beat_sync ─────────────────────────────
        if audio_file:
            if not os.path.exists(audio_file):
                print(f"  WARNING: Audio file not found: {audio_file!r} — using original audio.")
                shutil.copy2(pre_final, output_path)
            else:
                print(f"  Overlaying audio track: {audio_file}")
                r = ffmpeg_run(
                    [
                        "-y",
                        "-i", pre_final,
                        "-i", audio_file,
                        "-map", "0:v",
                        "-map", "1:a",
                        "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "192k",
                        "-shortest",
                        output_path,
                    ]
                )
                if r.returncode != 0:
                    print(f"  ERROR overlaying audio:\n{r.stderr[-400:]}")
                    return False
        else:
            shutil.copy2(pre_final, output_path)

        size_mb = os.path.getsize(output_path) / 1_048_576
        print(f"\n  Done. Output: {output_path}  ({size_mb:.1f} MB)")
        return True

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Video Editing Assistant — natural-language FFmpeg editor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("folder", help="Folder containing video clips")
    parser.add_argument(
        "--output", "-o", default="output.mp4", help="Output file (default: output.mp4)"
    )
    parser.add_argument(
        "--plan",
        "-p",
        default="edit_plan.json",
        help="JSON plan file path (default: edit_plan.json)",
    )
    parser.add_argument(
        "--load-plan",
        action="store_true",
        help="Load and re-run an existing plan file without re-analysing clips",
    )
    parser.add_argument(
        "--no-execute",
        action="store_true",
        help="Preview the plan but do not run FFmpeg",
    )
    args = parser.parse_args()

    print()
    print("  +------------------------------------------+")
    print("  |        Video Editing Assistant           |")
    print("  +------------------------------------------+")

    # ── Load existing plan or build a new one ────────────────────────────────
    if args.load_plan:
        if not os.path.exists(args.plan):
            print(f"\n  ERROR: Plan file not found: {args.plan}")
            sys.exit(1)
        with open(args.plan) as f:
            plan = json.load(f)
        print(f"\n  Loaded plan from '{args.plan}'  (type={plan.get('type')})")

    else:
        if not os.path.isdir(args.folder):
            print(f"\n  ERROR: '{args.folder}' is not a directory.")
            sys.exit(1)

        clips = analyze_folder(args.folder)
        valid = [c for c in clips if "error" not in c]
        if not valid:
            print("\n  No valid clips to process. Exiting.")
            sys.exit(1)

        total = sum(c.get("duration", 0) for c in valid)
        print(f"\n  {len(valid)} clip(s) found  |  total length {total:.1f}s ({fmt_time(total)})")

        # Interactive request prompt
        print()
        print("  " + "-" * 58)
        print("  What kind of edit do you want?")
        print()
        print("  Examples:")
        print("    cut out silences")
        print("    cut out silences below -40dB lasting 0.3 seconds")
        print("    make a 60 second highlight reel")
        print("    make a 2 minute montage")
        print("    sync cuts to the beat at 128 bpm")
        print("    sync to the beat of 'music.mp3' at 120 bpm")
        print("    trim from 0:30 to 2:45")
        print("    concatenate all clips")
        print("  " + "-" * 58)

        try:
            request = input("\n  Your request: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Aborted.")
            sys.exit(0)

        if not request:
            request = "concatenate all clips"
            print(f"  (no input — defaulting to: {request!r})")

        plan = parse_edit_request(request, clips)
        plan["clips_analyzed"] = clips  # store clip metadata in plan file

        with open(args.plan, "w") as f:
            json.dump(plan, f, indent=2)
        print(f"\n  Plan saved -> {args.plan}")

    # ── Preview ───────────────────────────────────────────────────────────────
    preview_edit_plan(plan)

    if args.no_execute:
        print("\n  (--no-execute: skipping FFmpeg execution)\n")
        return

    # ── Confirm and execute ───────────────────────────────────────────────────
    print()
    try:
        answer = input("  Execute this plan? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Aborted.")
        sys.exit(0)

    if answer not in ("", "y", "yes"):
        print(f"\n  Cancelled. Plan saved to '{args.plan}'.")
        print(f"  To re-run: python {Path(sys.argv[0]).name} {args.folder} "
              f"--load-plan --plan {args.plan}\n")
        return

    success = execute_edit_plan(plan, args.output)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
