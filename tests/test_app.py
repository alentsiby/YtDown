import io
import subprocess
import time
from pathlib import Path

import app


def setup_function():
    app.jobs.clear()
    app.upload_sessions.clear()
    app.last_cleanup = 0.0
    app.limiter.reset()


def test_youtube_url_validation():
    assert app.valid_youtube_url("https://youtu.be/example")
    assert app.valid_youtube_url("https://www.youtube.com/watch?v=example")
    assert not app.valid_youtube_url("https://youtube.com.example.org/watch?v=x")
    assert not app.valid_youtube_url("ftp://youtube.com/video")


def test_youtube_retries_are_bounded():
    options = app.make_ydl_options()
    assert options["retries"] == 0
    assert options["fragment_retries"] == 0
    assert options["extractor_retries"] == 0


def test_info_rate_limit():
    client = app.app.test_client()
    responses = [
        client.post("/api/info", json={"url": "not-a-youtube-url"})
        for _ in range(21)
    ]
    assert all(response.status_code == 400 for response in responses[:20])
    assert responses[20].status_code == 429


def test_info_rejects_non_youtube_host():
    response = app.app.test_client().post("/api/info", json={"url": "https://example.org/video"})
    assert response.status_code == 400


def test_host_bot_check_has_actionable_error_message():
    assert "local app" in app.media_error_message(
        RuntimeError("Sign in to confirm you’re not a bot")
    )
    assert "local app" not in app.media_error_message(RuntimeError("Video unavailable"))


def test_youtube_info_returns_actionable_access_denial(monkeypatch):
    class DeniedYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            raise RuntimeError("Sign in to confirm you’re not a bot")

    monkeypatch.setattr(app.yt_dlp, "YoutubeDL", DeniedYoutubeDL)
    response = app.app.test_client().post("/api/info", json={"url": "https://youtu.be/sample"})
    assert response.status_code == 422
    assert "automated check" in response.json["error"]


def test_info_reports_available_formats(monkeypatch):
    source_info = {
        "id": "sample",
        "title": "Authorized sample",
        "channel": "Test channel",
        "duration": 12,
        "webpage_url": "https://youtu.be/sample",
        "formats": [
            {"height": 720, "ext": "mp4", "vcodec": "avc1", "fps": 30, "tbr": 1500, "filesize": 1000},
            {"height": 720, "ext": "webm", "vcodec": "vp9", "fps": 60, "tbr": 3000, "filesize_approx": 2000},
            {"height": 0, "ext": "m4a", "vcodec": "none", "acodec": "mp4a"},
        ],
    }

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            assert download is False
            return source_info

    monkeypatch.setattr(app.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    response = app.app.test_client().post("/api/info", json={"url": "https://youtu.be/sample"})
    assert response.status_code == 200
    assert response.json["channel"] == "Test channel"
    assert response.json["formats"] == [
        {"height": 720, "ext": "webm", "vcodec": "vp9", "fps": 60, "tbr": 3000, "filesize": 2000}
    ]


def test_download_rejects_unknown_audio_format():
    response = app.app.test_client().post(
        "/api/download",
        json={"url": "https://youtu.be/sample", "output": "audio", "audio_format": "exe"},
    )
    assert response.status_code == 400


def test_download_accepts_inspected_resolution_range(monkeypatch):
    monkeypatch.setattr(app.executor, "submit", lambda *args: None)
    response = app.app.test_client().post(
        "/api/download",
        json={"url": "https://youtu.be/sample", "output": "video", "quality": "144"},
    )
    assert response.status_code == 202


def test_download_rejects_out_of_range_resolution():
    response = app.app.test_client().post(
        "/api/download",
        json={"url": "https://youtu.be/sample", "output": "video", "quality": "10000"},
    )
    assert response.status_code == 400


def test_download_rejects_when_another_job_is_active(monkeypatch):
    app.jobs["busy"] = {"status": "downloading", "created_at": time.time()}
    monkeypatch.setattr(app.executor, "submit", lambda *args: (_ for _ in ()).throw(AssertionError("must not queue")))
    response = app.app.test_client().post(
        "/api/download",
        json={"url": "https://youtu.be/sample", "output": "audio"},
    )
    assert response.status_code == 429


def test_cleanup_removes_expired_job_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DOWNLOADS", tmp_path)
    output = tmp_path / "expired.mp4"
    output.write_bytes(b"sample")
    app.jobs["expired"] = {
        "status": "complete",
        "created_at": time.time() - app.JOB_TTL_SECONDS - 1,
        "filename": output.name,
        "file_path": str(output),
    }

    app.cleanup_expired_jobs()

    assert "expired" not in app.jobs
    assert not output.exists()


def test_upload_inspection_and_audio_processing_success(monkeypatch):
    monkeypatch.setattr(
        app,
        "probe_media",
        lambda path: {
            "format": {"format_name": "mov,mp4", "duration": "2"},
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
            "duration": 2,
        },
    )

    def run_ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b"processed audio")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(app.subprocess, "run", run_ffmpeg)
    monkeypatch.setattr(app.executor, "submit", lambda function, *args: function(*args))
    client = app.app.test_client()
    inspected = client.post(
        "/api/uploads/inspect",
        data={"authorized": "true", "file": (io.BytesIO(b"owned media"), "my recording.mp4")},
        content_type="multipart/form-data",
    )
    assert inspected.status_code == 200
    assert inspected.json["filename"] == "my_recording.mp4"
    processed = client.post(
        f"/api/uploads/{inspected.json['upload_id']}/process",
        json={"operation": "audio", "audio_format": "mp3"},
    )
    assert processed.status_code == 202
    job = client.get(f"/api/jobs/{processed.json['job_id']}").json
    assert job["status"] == "complete"
    assert job["filename"] == "processed.mp3"
    assert client.get(job["download_url"]).status_code == 200


def test_upload_requires_permission_confirmation():
    response = app.app.test_client().post(
        "/api/uploads/inspect",
        data={"file": (io.BytesIO(b"data"), "owned.mp4")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_upload_session_limit_rejects_new_upload():
    app.upload_sessions["pending-1"] = {"created_at": time.time()}
    app.upload_sessions["pending-2"] = {"created_at": time.time()}
    response = app.app.test_client().post(
        "/api/uploads/inspect",
        data={"authorized": "true", "file": (io.BytesIO(b"data"), "media.mp4")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 429


def test_discard_upload_removes_temp_workspace(tmp_path):
    workspace = tmp_path / "discarded-upload"
    workspace.mkdir()
    (workspace / "source.mp4").write_bytes(b"temporary")
    app.upload_sessions["discarded-upload"] = {"workspace": workspace, "created_at": time.time()}
    response = app.app.test_client().delete("/api/uploads/discarded-upload")
    assert response.status_code == 204
    assert "discarded-upload" not in app.upload_sessions
    assert not workspace.exists()


def test_upload_rejects_unsupported_extension():
    response = app.app.test_client().post(
        "/api/uploads/inspect",
        data={"authorized": "true", "file": (io.BytesIO(b"data"), "notes.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 415


def test_upload_rejects_malformed_media_and_cleans_temp_dir(monkeypatch):
    monkeypatch.setattr(app, "probe_media", lambda path: (_ for _ in ()).throw(ValueError("Media probe rejected the file.")))
    response = app.app.test_client().post(
        "/api/uploads/inspect",
        data={"authorized": "true", "file": (io.BytesIO(b"not media"), "broken.mp4")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    assert response.json["error"] == "Media probe rejected the file."
    assert not app.upload_sessions


def test_upload_rejects_request_over_size_limit(monkeypatch):
    monkeypatch.setitem(app.app.config, "MAX_CONTENT_LENGTH", 1024)
    response = app.app.test_client().post(
        "/api/uploads/inspect",
        data={"authorized": "true", "file": (io.BytesIO(b"x" * 2048), "large.mp4")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert "512 MiB" in response.json["error"]


def test_ffprobe_timeout_is_reported(monkeypatch, tmp_path):
    media_path = tmp_path / "media.mp4"
    media_path.write_bytes(b"sample")

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(app.subprocess, "run", timeout)
    try:
        app.probe_media(media_path)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("FFprobe timeout should propagate to the upload route")


def test_probe_rejects_invalid_json_and_no_media_streams(monkeypatch, tmp_path):
    media_path = tmp_path / "media.mp4"
    media_path.write_bytes(b"sample")
    for stdout in ("not json", '{"format":{"duration":"2"},"streams":[]}'):
        monkeypatch.setattr(
            app.subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=""),
        )
        try:
            app.probe_media(media_path)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid probe results must be rejected")


def test_upload_route_catches_ffprobe_timeout_and_cleans_workspace(monkeypatch):
    def timeout(path):
        raise subprocess.TimeoutExpired("ffprobe", app.FFPROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(app, "probe_media", timeout)
    response = app.app.test_client().post(
        "/api/uploads/inspect",
        data={"authorized": "true", "file": (io.BytesIO(b"media"), "timeout.mp4")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    assert "timed out" in response.json["error"]
    assert not app.upload_sessions


def test_probe_media_parses_valid_ffprobe_output(monkeypatch, tmp_path):
    media_path = tmp_path / "media.mp4"
    media_path.write_bytes(b"sample")
    probe_json = '{"format":{"duration":"3.5","format_name":"mov,mp4"},"streams":[{"codec_type":"video","codec_name":"h264","height":720}]}'
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=probe_json, stderr=""),
    )
    result = app.probe_media(media_path)
    assert result["duration"] == 3.5
    assert result["streams"][0]["codec_name"] == "h264"


def test_upload_processing_ffmpeg_failure_cleans_workspace(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    upload = {
        "workspace": tmp_path,
        "source_path": source,
        "media_info": {"streams": [{"codec_type": "video"}]},
    }
    app.jobs["ffmpeg-failure"] = {"status": "queued"}
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr="unsupported codec"),
    )
    app.run_upload_job("ffmpeg-failure", upload, "video", "original")
    assert app.jobs["ffmpeg-failure"]["status"] == "error"
    assert "unsupported codec" not in app.jobs["ffmpeg-failure"]["error"]
    assert not tmp_path.exists()


def test_upload_processing_timeout_cleans_workspace(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    upload = {
        "workspace": tmp_path,
        "source_path": source,
        "media_info": {"streams": [{"codec_type": "video"}]},
    }
    app.jobs["ffmpeg-timeout"] = {"status": "queued"}

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(app.subprocess, "run", timeout)
    app.run_upload_job("ffmpeg-timeout", upload, "video", "original")
    assert app.jobs["ffmpeg-timeout"]["status"] == "error"
    assert "timed out" in app.jobs["ffmpeg-timeout"]["error"]
    assert not tmp_path.exists()


def test_cleanup_removes_expired_upload_session(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "UPLOAD_ROOT", tmp_path)
    workspace = tmp_path / "expired-upload"
    workspace.mkdir()
    (workspace / "source.mp4").write_bytes(b"temporary")
    app.upload_sessions["expired-upload"] = {
        "workspace": workspace,
        "created_at": time.time() - app.JOB_TTL_SECONDS - 1,
    }
    app.cleanup_expired_jobs()
    assert "expired-upload" not in app.upload_sessions
    assert not workspace.exists()
