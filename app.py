from __future__ import annotations

import os
import re
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix


ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
executor = ThreadPoolExecutor(max_workers=1)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120 per hour"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
MAX_ACTIVE_DOWNLOADS = 1
JOB_TTL_SECONDS = 60 * 60
MAX_DOWNLOAD_BYTES = 2 * 1024**3
MAX_DURATION_SECONDS = 2 * 60 * 60
last_cleanup = 0.0

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def valid_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and host in YOUTUBE_HOSTS and bool(parsed.path.strip("/"))
    except ValueError:
        return False


def duration_filter(info: dict, *, incomplete: bool = False) -> str | None:
    duration = info.get("duration")
    if duration and duration > MAX_DURATION_SECONDS:
        return "Videos longer than two hours are not supported."
    return None


def make_ydl_options(**options) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "socket_timeout": 20,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "match_filter": duration_filter,
        "outtmpl": str(DOWNLOADS / "%(title).180B [%(id)s].%(ext)s"),
        **options,
    }


def cleanup_expired_jobs() -> None:
    global last_cleanup
    now = time.time()
    with jobs_lock:
        if now - last_cleanup < 60:
            return
        last_cleanup = now
        expired = [
            job_id
            for job_id, job in jobs.items()
            if job["status"] not in {"queued", "downloading", "processing"}
            and now - job["created_at"] >= JOB_TTL_SECONDS
        ]
        for job_id in expired:
            job = jobs.pop(job_id)
            filename = job.get("filename")
            if filename:
                (DOWNLOADS / filename).unlink(missing_ok=True)


@app.before_request
def expire_old_jobs() -> None:
    cleanup_expired_jobs()


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/info")
@limiter.limit("20 per minute")
def video_info():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not valid_youtube_url(url):
        return jsonify(error="Enter a valid YouTube video URL."), 400

    try:
        with yt_dlp.YoutubeDL(make_ydl_options(skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
        best_by_height = {}
        for fmt in info.get("formats", []):
            if not fmt.get("height") or fmt.get("vcodec") == "none":
                continue
            height = int(fmt["height"])
            if height not in best_by_height or (fmt.get("tbr") or 0) > (best_by_height[height].get("tbr") or 0):
                best_by_height[height] = fmt
        formats = [
            {
                "height": height,
                "ext": fmt.get("ext"),
                "vcodec": fmt.get("vcodec"),
                "fps": fmt.get("fps"),
                "tbr": fmt.get("tbr"),
                "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            }
            for height, fmt in sorted(best_by_height.items(), reverse=True)
        ]
        return jsonify(
            id=info.get("id"),
            title=info.get("title", "YouTube video"),
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
            channel=info.get("channel"),
            source_url=info.get("webpage_url"),
            formats=formats,
        )
    except Exception as exc:
        app.logger.warning("Video inspection failed: %s", exc)
        return jsonify(error="Could not read this video. Check the URL and try again."), 422


def run_download(job_id: str, url: str, output: str, quality: str, audio_format: str) -> None:
    def update(status: str, **values) -> None:
        with jobs_lock:
            jobs[job_id].update(status=status, **values)

    def progress_hook(data: dict) -> None:
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            percent = round(data.get("downloaded_bytes", 0) * 100 / total) if total else None
            update("downloading", percent=percent, speed=data.get("_speed_str", ""), eta=data.get("_eta_str", ""))
        elif data.get("status") == "finished":
            update("processing", percent=100)

    if output == "audio":
        options = {"format": "bestaudio/best", "progress_hooks": [progress_hook]}
        if audio_format != "original":
            audio_postprocessor = {"key": "FFmpegExtractAudio", "preferredcodec": audio_format}
            if audio_format == "mp3":
                audio_postprocessor["preferredquality"] = "0"
            options["postprocessors"] = [audio_postprocessor]
    else:
        height = int(quality)
        video_format = f"bestvideo[height<={height}]"
        if output == "video":
            options = {"format": f"{video_format}/best[height<={height}]", "progress_hooks": [progress_hook]}
        else:
            options = {
                "format": f"{video_format}+bestaudio/best[height<={height}]",
                "merge_output_format": "mp4",
                "progress_hooks": [progress_hook],
            }

    try:
        with yt_dlp.YoutubeDL(make_ydl_options(**options)) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if output == "combined":
                filename = str(Path(filename).with_suffix(".mp4"))
            elif output == "audio" and audio_format != "original":
                filename = str(Path(filename).with_suffix(f".{audio_format}"))
        if not Path(filename).is_file():
            raise FileNotFoundError("The downloaded file could not be located.")
        update("complete", percent=100, filename=Path(filename).name)
    except Exception as exc:
        app.logger.warning("Download job %s failed: %s", job_id, exc)
        update("error", error="Download failed. The source may be unavailable or incompatible with this format.")


@app.post("/api/download")
@limiter.limit("5 per hour")
def start_download():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    output = data.get("output")
    quality = str(data.get("quality", "1080"))
    audio_format = data.get("audio_format", "original")
    if not valid_youtube_url(url):
        return jsonify(error="Enter a valid YouTube video URL."), 400
    if output not in {"video", "audio", "combined"}:
        return jsonify(error="Choose a valid output type."), 400
    if audio_format not in {"original", "mp3", "m4a", "wav", "flac"}:
        return jsonify(error="Choose a valid audio format."), 400
    if not re.fullmatch(r"\d{3,5}", quality) or not 144 <= int(quality) <= 8640:
        return jsonify(error="Choose a valid video quality."), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        active_jobs = sum(job["status"] in {"queued", "downloading", "processing"} for job in jobs.values())
        if active_jobs >= MAX_ACTIVE_DOWNLOADS:
            return jsonify(error="A download is already in progress. Try again when it finishes."), 429
        jobs[job_id] = {"status": "queued", "percent": 0, "created_at": time.time()}
    executor.submit(run_download, job_id, url, output, quality, audio_format)
    return jsonify(job_id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify(error="Download not found."), 404
    result = dict(job)
    if result.get("status") == "complete":
        result["download_url"] = f"/api/files/{job_id}"
    return jsonify(result)


@app.get("/api/files/<job_id>")
def download_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        filename = job.get("filename") if job and job.get("status") == "complete" else None
    if not filename:
        return jsonify(error="File not found."), 404
    return send_from_directory(DOWNLOADS, filename, as_attachment=True)


@app.errorhandler(429)
def rate_limit_error(_error):
    return jsonify(error="Rate limit reached. Please wait before trying again."), 429


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)