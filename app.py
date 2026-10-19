from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, render_template, request, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)
UPLOAD_ROOT = Path(tempfile.gettempdir()) / "frame-extract-uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 64 * 1024
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
MAX_PENDING_UPLOADS = 2
JOB_TTL_SECONDS = 60 * 60
MAX_DOWNLOAD_BYTES = 2 * 1024**3
MAX_DURATION_SECONDS = 2 * 60 * 60
FFPROBE_TIMEOUT_SECONDS = 20
FFMPEG_TIMEOUT_SECONDS = 5 * 60
MAX_PROCESSED_BYTES = 512 * 1024 * 1024
SUPPORTED_UPLOAD_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpeg", ".mpg", ".3gp",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".aiff",
}
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
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "match_filter": duration_filter,
        "outtmpl": str(DOWNLOADS / "%(title).180B [%(id)s].%(ext)s"),
        **options,
    }


def media_error_message(error: Exception) -> str:
    message = str(error).lower()
    if "not a bot" in message or "confirm you’re not a bot" in message:
        return (
            "YouTube blocked requests from this hosting server with an automated check. "
            "Try the local app or an authorized media source that permits server access."
        )
    if "timed out" in message or "timeout" in message:
        return "The source took too long to respond. Try again later or use an authorized media file."
    if "sign in" in message or "private video" in message or "age-restricted" in message:
        return "This source requires access that the app cannot provide. Use a public, authorized source."
    return (
        "The server could not retrieve this video's information. The video may be unavailable "
        "to the server, or the source may block requests from this host. Try again later or "
        "run the app locally."
    )


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
            workspace = job.get("workspace")
            if workspace:
                shutil.rmtree(workspace, ignore_errors=True)
            elif job.get("file_path"):
                Path(job["file_path"]).unlink(missing_ok=True)
        expired_uploads = [
            upload_id
            for upload_id, upload in upload_sessions.items()
            if now - upload["created_at"] >= JOB_TTL_SECONDS
        ]
        for upload_id in expired_uploads:
            upload = upload_sessions.pop(upload_id)
            shutil.rmtree(upload["workspace"], ignore_errors=True)
    for workspace in UPLOAD_ROOT.iterdir():
        if workspace.is_dir() and now - workspace.stat().st_mtime >= JOB_TTL_SECONDS:
            shutil.rmtree(workspace, ignore_errors=True)


@app.before_request
def expire_old_jobs() -> None:
    cleanup_expired_jobs()


@app.get("/")
def index():
    return render_template("index.html")


upload_sessions: dict[str, dict] = {}


def probe_media(path: Path) -> dict:
    result = subprocess.run(
        [
            os.environ.get("FFPROBE_BIN", "ffprobe"),
            "-v", "error",
            "-show_entries", "format=duration,format_name,size:stream=codec_type,codec_name,width,height,bit_rate,avg_frame_rate",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("Media probe rejected the file.")
    try:
        media_info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Media probe returned invalid data.") from exc
    streams = [stream for stream in media_info.get("streams", []) if stream.get("codec_type") in {"video", "audio"}]
    if not streams:
        raise ValueError("The file contains no supported audio or video streams.")
    duration = float(media_info.get("format", {}).get("duration") or 0)
    if duration > MAX_DURATION_SECONDS:
        raise ValueError("Media exceeds the two-hour processing limit.")
    return {"format": media_info.get("format", {}), "streams": streams, "duration": duration}


@app.post("/api/uploads/inspect")
@limiter.limit("5 per hour")
def inspect_upload():
    if request.form.get("authorized") != "true":
        return jsonify(error="Confirm that you own this media or have permission to process it."), 400
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify(error="Choose a media file to upload."), 400
    original_name = secure_filename(uploaded.filename)
    suffix = Path(original_name).suffix.lower()
    if not suffix or suffix not in SUPPORTED_UPLOAD_EXTENSIONS:
        return jsonify(error="This file type is not supported. Choose a common audio or video file."), 415

    upload_id = uuid.uuid4().hex
    workspace = Path(tempfile.mkdtemp(prefix=f"{upload_id}-", dir=UPLOAD_ROOT))
    source_path = workspace / f"source{suffix}"
    with jobs_lock:
        if len(upload_sessions) >= MAX_PENDING_UPLOADS:
            shutil.rmtree(workspace, ignore_errors=True)
            return jsonify(error="Upload slots are full. Process or wait for an existing upload to expire."), 429
        upload_sessions[upload_id] = {
            "workspace": workspace,
            "source_path": source_path,
            "original_name": original_name,
            "created_at": time.time(),
        }
    try:
        uploaded.save(source_path)
        if source_path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("Upload exceeds the 512 MiB size limit.")
        media_info = probe_media(source_path)
    except subprocess.TimeoutExpired:
        with jobs_lock:
            upload_sessions.pop(upload_id, None)
        shutil.rmtree(workspace, ignore_errors=True)
        return jsonify(error="Media inspection timed out. Try a smaller or shorter file."), 422
    except ValueError as exc:
        with jobs_lock:
            upload_sessions.pop(upload_id, None)
        shutil.rmtree(workspace, ignore_errors=True)
        return jsonify(error=str(exc)), 422
    except Exception as exc:
        app.logger.warning("Upload inspection failed: %s", exc)
        with jobs_lock:
            upload_sessions.pop(upload_id, None)
        shutil.rmtree(workspace, ignore_errors=True)
        return jsonify(error="Could not inspect this upload."), 422

    with jobs_lock:
        upload_sessions[upload_id]["media_info"] = media_info
    return jsonify(
        upload_id=upload_id,
        filename=original_name,
        duration=media_info["duration"],
        format=media_info["format"].get("format_name"),
        streams=media_info["streams"],
    )


def run_upload_job(job_id: str, upload: dict, operation: str, audio_format: str) -> None:
    workspace = upload["workspace"]
    source_path = upload["source_path"]
    video_stream = next((stream for stream in upload["media_info"]["streams"] if stream["codec_type"] == "video"), None)
    audio_stream = next((stream for stream in upload["media_info"]["streams"] if stream["codec_type"] == "audio"), None)

    if operation == "video":
        output_path = workspace / f"processed{source_path.suffix}"
        command = ["-map", "0:v:0", "-an", "-c:v", "copy"]
    elif operation == "combined":
        output_path = workspace / f"processed{source_path.suffix}"
        command = ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
    else:
        extension = "mka" if audio_format == "original" else audio_format
        output_path = workspace / f"processed.{extension}"
        command = ["-map", "0:a:0", "-vn"]
        codecs = {
            "original": ["-c:a", "copy"],
            "mp3": ["-c:a", "libmp3lame", "-q:a", "0"],
            "m4a": ["-c:a", "aac", "-b:a", "192k"],
            "flac": ["-c:a", "flac"],
            "wav": ["-c:a", "pcm_s16le"],
        }
        command.extend(codecs[audio_format])

    try:
        if operation == "video" and not video_stream:
            raise ValueError("No video stream is present.")
        if operation == "audio" and not audio_stream:
            raise ValueError("No audio stream is present.")
        if operation == "combined" and (not video_stream or not audio_stream):
            raise ValueError("Combined output requires both audio and video streams.")
        with jobs_lock:
            jobs[job_id]["status"] = "processing"
        result = subprocess.run(
            [
                os.environ.get("FFMPEG_BIN", "ffmpeg"),
                "-hide_banner", "-v", "error", "-nostdin", "-y",
                "-i", str(source_path),
                *command,
                "-fs", str(MAX_PROCESSED_BYTES),
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError(result.stderr[-2000:] or "FFmpeg failed to produce output.")
        if output_path.stat().st_size >= MAX_PROCESSED_BYTES:
            raise ValueError("Processed output exceeds the 512 MiB limit. Choose a shorter file or smaller output.")
        with jobs_lock:
            jobs[job_id].update(
                status="complete",
                percent=100,
                filename=output_path.name,
                file_path=str(output_path),
            )
    except subprocess.TimeoutExpired:
        shutil.rmtree(workspace, ignore_errors=True)
        with jobs_lock:
            jobs[job_id].update(status="error", error="Processing timed out. Try a smaller or shorter file.", workspace=None)
    except ValueError as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        with jobs_lock:
            jobs[job_id].update(status="error", error=str(exc), workspace=None)
    except Exception as exc:
        app.logger.warning("Upload processing job %s failed: %s", job_id, exc)
        shutil.rmtree(workspace, ignore_errors=True)
        with jobs_lock:
            jobs[job_id].update(status="error", error="FFmpeg could not process this media file.", workspace=None)


@app.post("/api/uploads/<upload_id>/process")
@limiter.limit("5 per hour")
def process_upload(upload_id: str):
    data = request.get_json(silent=True) or {}
    operation = data.get("operation")
    audio_format = data.get("audio_format", "original")
    if operation not in {"video", "audio", "combined"}:
        return jsonify(error="Choose video-only, audio-only, or combined output."), 400
    if audio_format not in {"original", "mp3", "m4a", "flac", "wav"}:
        return jsonify(error="Choose a supported audio format."), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        upload = upload_sessions.get(upload_id)
        if upload is None:
            return jsonify(error="Upload expired or not found. Upload the file again."), 404
        active_jobs = sum(job["status"] in {"queued", "downloading", "processing"} for job in jobs.values())
        if active_jobs >= MAX_ACTIVE_DOWNLOADS:
            return jsonify(error="A media job is already running. Try again when it finishes."), 429
        upload_sessions.pop(upload_id)
        jobs[job_id] = {
            "status": "queued",
            "percent": None,
            "created_at": time.time(),
            "workspace": upload["workspace"],
        }
    executor.submit(run_upload_job, job_id, upload, operation, audio_format)
    return jsonify(job_id=job_id), 202


@app.delete("/api/uploads/<upload_id>")
@limiter.limit("20 per minute")
def discard_upload(upload_id: str):
    with jobs_lock:
        upload = upload_sessions.pop(upload_id, None)
    if upload:
        shutil.rmtree(upload["workspace"], ignore_errors=True)
    return "", 204


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
        return jsonify(error=media_error_message(exc)), 422


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
        update("complete", percent=100, filename=Path(filename).name, file_path=str(Path(filename).resolve()))
    except Exception as exc:
        app.logger.warning("Download job %s failed: %s", job_id, exc)
        update("error", error=media_error_message(exc))


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
    result.pop("workspace", None)
    result.pop("file_path", None)
    if result.get("status") == "complete":
        result["download_url"] = f"/api/files/{job_id}"
    return jsonify(result)


@app.get("/api/files/<job_id>")
def download_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        file_path = job.get("file_path") if job and job.get("status") == "complete" else None
        filename = job.get("filename") if job and job.get("status") == "complete" else None
    if not file_path or not filename or not Path(file_path).is_file():
        return jsonify(error="File not found."), 404
    return send_file(file_path, as_attachment=True, download_name=filename)


@app.errorhandler(429)
def rate_limit_error(_error):
    return jsonify(error="Rate limit reached. Please wait before trying again."), 429


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify(error="Upload is too large. The maximum file size is 512 MiB."), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)