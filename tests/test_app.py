import time

import app


def setup_function():
    app.jobs.clear()
    app.last_cleanup = 0.0
    app.limiter.reset()


def test_youtube_url_validation():
    assert app.valid_youtube_url("https://youtu.be/example")
    assert app.valid_youtube_url("https://www.youtube.com/watch?v=example")
    assert not app.valid_youtube_url("https://youtube.com.example.org/watch?v=x")
    assert not app.valid_youtube_url("ftp://youtube.com/video")


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
    }

    app.cleanup_expired_jobs()

    assert "expired" not in app.jobs
    assert not output.exists()
