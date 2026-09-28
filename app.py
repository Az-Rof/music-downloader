"""
Music Downloader — local web server.

Run:
    python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request, send_file

from downloader import (
    BITRATE_CHOICES,
    BASE_DIR,
    DOWNLOADS_DIR,
    QUALITY_LABELS,
    DownloadEngine,
)

LOG_FILE = BASE_DIR / "music-downloader.log"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler(), _file_handler],
)
logger = logging.getLogger("music-downloader")

app = Flask(__name__)

# Quality option order in UI (best first)
QUALITY_ORDER = ["auto", "320", "256", "192", "128"]

ALLOWED_DOMAINS = ("spotify.com", "spotify.link", "youtube.com", "youtu.be")
MAX_URL_LENGTH = 2048

engine = DownloadEngine()
engine.start()


def _valid_url(raw) -> Optional[str]:
    """Simple validation: must be an http(s) URL from a known domain."""
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        return None
    if not url.startswith(("http://", "https://")):
        return None
    lowered = url.lower()
    if not any(domain in lowered for domain in ALLOWED_DOMAINS):
        return None
    return url


# ============================================================
# Pages
# ============================================================

@app.get("/")
def index():
    qualities = [(key, QUALITY_LABELS.get(key, key)) for key in QUALITY_ORDER]
    return render_template("index.html", qualities=qualities)


# ============================================================
# API
# ============================================================

@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    url = _valid_url(data.get("url"))
    if url is None:
        return jsonify({
            "error": (
                "Invalid link. Use a song/playlist/album link from "
                "Spotify or YouTube / YouTube Music."
            )
        }), 400

    quality = data.get("quality", "auto")
    if quality not in BITRATE_CHOICES:
        quality = "auto"

    task = engine.submit(url, quality)
    logger.info("New task %s: %s (quality=%s)", task.task_id, url, quality)
    return jsonify(task.to_dict())


@app.get("/api/status/<task_id>")
def api_status(task_id):
    task = engine.get_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found."}), 404
    return jsonify(task)


@app.get("/api/history")
def api_history():
    limit = request.args.get("limit", default=20, type=int)
    limit = max(1, min(limit, 50))
    return jsonify({"tasks": engine.list_tasks(limit)})


@app.get("/api/logs")
def api_logs():
    """Send recent log lines for the diagnostic panel in the UI."""
    limit = request.args.get("limit", default=300, type=int)
    limit = max(1, min(limit, 1000))
    try:
        _file_handler.flush()
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-limit:]
        return jsonify({"lines": [line.rstrip("\r\n") for line in lines]})
    except OSError as exc:
        logger.exception("Failed to read log file")
        return jsonify({"lines": [], "error": str(exc)}), 500


@app.post("/api/logs/clear")
def api_logs_clear():
    """Clear the active log without deleting backup log files."""
    try:
        _file_handler.acquire()
        try:
            _file_handler.flush()
            stream = _file_handler.stream
            if stream is not None:
                stream.seek(0)
                stream.truncate(0)
                stream.flush()
        finally:
            _file_handler.release()
        logger.info("Application log cleared from web interface")
        return jsonify({"ok": True})
    except OSError as exc:
        logger.exception("Failed to clear log file")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/file")
def api_file():
    """Serve an MP3 file from the downloads folder (download button in UI)."""
    rel = request.args.get("path", "").strip()
    if not rel:
        abort(404)

    base = DOWNLOADS_DIR.resolve()
    try:
        target = (base / rel).resolve()
    except (OSError, ValueError):
        abort(404)

    # Security: file must be INSIDE the downloads folder (anti path-traversal)
    if base not in target.parents:
        abort(404)
    if not target.is_file():
        abort(404)

    return send_file(target, as_attachment=True)


@app.get("/api/open-downloads")
def api_open_downloads():
    """Open the downloads folder in the system file explorer."""
    try:
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(DOWNLOADS_DIR))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(DOWNLOADS_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(DOWNLOADS_DIR)])
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    print()
    print("  ==============================================")
    print("   Music Downloader")
    print("   Buka di browser: http://127.0.0.1:5000")
    print("  ==============================================")
    print()
    # use_reloader=False so the download worker is not created twice
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
