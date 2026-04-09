#!/usr/bin/env python3
"""
app.py — Video Editing Assistant Web App

Run:
    pip install flask
    python app.py

Then open http://localhost:5000 in your browser.
Your phone (on the same Wi-Fi) can connect at the Network URL printed on startup.
"""

import json
import os
import queue
import shutil
import socket
import sys
import threading
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

# Import core logic from the CLI module
sys.path.insert(0, str(Path(__file__).parent))
from video_editor import analyze_folder, execute_edit_plan, parse_edit_request

app = Flask(__name__)
# Allow large video file uploads (up to 8 GB)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}

WORK_DIR = Path(__file__).parent / "vedit_workspace"
WORK_DIR.mkdir(exist_ok=True)

# In-memory session store — fine for a single-user local app
_sessions: dict = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _new_session() -> tuple[str, dict]:
    sid = uuid.uuid4().hex[:12]
    sess_dir = WORK_DIR / sid
    (sess_dir / "clips").mkdir(parents=True, exist_ok=True)
    sess = {
        "dir": sess_dir,
        "clips": [],
        "plan": None,
        "output": sess_dir / "output.mp4",
        "q": queue.Queue(),
        "status": "idle",
    }
    with _lock:
        _sessions[sid] = sess
    return sid, sess


def _get(sid: str) -> dict | None:
    with _lock:
        return _sessions.get(sid)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/manifest.json")
def manifest():
    """PWA manifest so the app can be installed to a phone home screen."""
    return jsonify(
        {
            "name": "Video Editor",
            "short_name": "VideoEdit",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0f0f18",
            "theme_color": "#7c6af7",
            "description": "Natural-language video editing assistant",
            "icons": [
                {
                    "src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎬</text></svg>",
                    "sizes": "any",
                    "type": "image/svg+xml",
                }
            ],
        }
    )


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """
    Analyze video clips.
    Accepts either:
      - form field 'folder_path' with a server-side directory path, or
      - multipart 'files' with uploaded video files.
    Returns session_id + list of clip metadata dicts.
    """
    sid, sess = _new_session()
    clips_dir = sess["dir"] / "clips"

    folder = request.form.get("folder_path", "").strip()
    files = request.files.getlist("files")

    if folder:
        if not Path(folder).is_dir():
            return jsonify(error=f"Not a directory: {folder}"), 400
        analyze_path = folder
    elif files:
        saved = 0
        for f in files:
            if f.filename and Path(f.filename).suffix.lower() in VIDEO_EXTS:
                safe_name = Path(f.filename).name
                f.save(clips_dir / safe_name)
                saved += 1
        if not saved:
            return jsonify(error="No recognised video files in upload"), 400
        analyze_path = str(clips_dir)
    else:
        return jsonify(error="Provide folder_path or upload files"), 400

    clips = analyze_folder(analyze_path)
    sess["clips"] = clips
    valid_count = sum(1 for c in clips if "error" not in c)
    return jsonify(session_id=sid, clips=clips, valid_count=valid_count)


@app.route("/api/plan", methods=["POST"])
def api_plan():
    """Generate an edit plan from a natural-language request."""
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id", "")
    req_text = (body.get("request") or "concatenate all clips").strip()

    sess = _get(sid)
    if not sess:
        return jsonify(error="Session not found"), 404

    plan = parse_edit_request(req_text, sess["clips"])
    plan["clips_analyzed"] = sess["clips"]
    sess["plan"] = plan

    plan_file = sess["dir"] / "edit_plan.json"
    plan_file.write_text(json.dumps(plan, indent=2))

    return jsonify(plan=plan)


@app.route("/api/plan/<sid>", methods=["PUT"])
def api_update_plan(sid):
    """Replace the stored plan with an edited version (from the JSON editor)."""
    sess = _get(sid)
    if not sess:
        return jsonify(error="Session not found"), 404

    body = request.get_json(silent=True) or {}
    plan = body.get("plan")
    if not plan:
        return jsonify(error="No plan provided"), 400

    sess["plan"] = plan
    (sess["dir"] / "edit_plan.json").write_text(json.dumps(plan, indent=2))
    return jsonify(ok=True)


@app.route("/api/execute/<sid>", methods=["POST"])
def api_execute(sid):
    """Start plan execution in a background thread."""
    sess = _get(sid)
    if not sess:
        return jsonify(error="Session not found"), 404
    if not sess.get("plan"):
        return jsonify(error="No plan to execute"), 400

    # Drain stale messages from a previous run
    while not sess["q"].empty():
        try:
            sess["q"].get_nowait()
        except queue.Empty:
            break

    sess["status"] = "running"

    def _run():
        def emit(msg: str):
            sess["q"].put({"type": "log", "message": msg})

        try:
            ok = execute_edit_plan(sess["plan"], str(sess["output"]), on_progress=emit)
            if ok:
                mb = os.path.getsize(str(sess["output"])) / 1_048_576
                sess["q"].put(
                    {
                        "type": "done",
                        "message": f"Complete — {mb:.1f} MB",
                        "download_url": f"/api/download/{sid}",
                    }
                )
                sess["status"] = "done"
            else:
                sess["q"].put({"type": "error", "message": "Execution failed — check server logs."})
                sess["status"] = "error"
        except Exception as exc:
            sess["q"].put({"type": "error", "message": str(exc)})
            sess["status"] = "error"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/progress/<sid>")
def api_progress(sid):
    """
    Server-Sent Events stream for real-time execution progress.
    The browser opens this with EventSource and receives JSON-encoded messages.
    """
    sess = _get(sid)
    if not sess:
        return Response(
            'data: {"type":"error","message":"session not found"}\n\n',
            mimetype="text/event-stream",
        )

    def _gen():
        q = sess["q"]
        while True:
            try:
                msg = q.get(timeout=25)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                # Keep connection alive
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/download/<sid>")
def api_download(sid):
    """Download the finished output file."""
    sess = _get(sid)
    if not sess:
        return jsonify(error="Session not found"), 404
    out = sess.get("output")
    if not out or not Path(str(out)).exists():
        return jsonify(error="Output not ready yet"), 404
    return send_file(
        str(out),
        as_attachment=True,
        download_name="edited_video.mp4",
        mimetype="video/mp4",
    )


@app.route("/api/cleanup/<sid>", methods=["DELETE"])
def api_cleanup(sid):
    """Delete the workspace for a session (frees disk space)."""
    sess = _get(sid)
    if not sess:
        return jsonify(error="Session not found"), 404
    shutil.rmtree(str(sess["dir"]), ignore_errors=True)
    with _lock:
        _sessions.pop(sid, None)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _local_ip() -> str:
    """Best-effort detection of the machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Video Editing Assistant — web UI")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = parser.parse_args()

    ip = _local_ip()
    url = f"http://localhost:{args.port}"

    print()
    print("  ┌────────────────────────────────────────────────┐")
    print("  │        Video Editing Assistant — Web UI        │")
    print("  ├────────────────────────────────────────────────┤")
    print(f"  │  Local  : {url:<38}│")
    print(f"  │  Network: http://{ip}:{args.port:<29}│")
    print("  │                                                │")
    print("  │  Open the Network URL on your phone            │")
    print("  │  (both devices must be on the same Wi-Fi)      │")
    print("  └────────────────────────────────────────────────┘")
    print()

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
