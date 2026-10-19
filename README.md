# Frame / Extract

A small Flask application for inspecting supported YouTube video formats and saving video-only, audio-only, or combined output. Use it only for content you own or are authorized to process. This project does not bypass private-video restrictions, DRM, authentication, or other access controls.

## Features

- Inspects available video resolutions and reports the selected source codec, frame rate, bitrate, and estimated size when provided by the source.
- Downloads video-only or combines video and audio with FFmpeg.
- Keeps the best available audio in its original format, or converts it to MP3, M4A, FLAC, or WAV using FFmpeg. MP3 is lossy; WAV does not restore quality missing from the source.
- Reports real yt-dlp transfer progress and estimated time when available.
- Limits work to one active download, five download requests per client IP per hour, twenty inspections per minute, a two-hour maximum duration, and a 2 GiB maximum file size. Completed files and terminal job records expire after one hour when a later request triggers cleanup.

## Limitations and source support

The app uses yt-dlp to inspect and retrieve public URLs it supports. It is not an official YouTube download integration, and support can change as the source changes. YouTube's official API is not a general-purpose video download API. Cloud hosts such as Render may be blocked by YouTube's automated checks even when the same URL works locally; the app reports this and does not attempt to bypass it. Do not assume every URL works, and follow the source platform's terms and applicable law. Metadata inspection alone does not grant permission to download.

The app has no user accounts. It applies in-memory IP rate limits and allows one active job; configure `RATELIMIT_STORAGE_URI` for shared rate-limit storage if running multiple instances. The app trusts one reverse proxy for client IP and scheme headers, as configured for Render. Review this setting for other deployment topologies. Add authentication, monitoring, and durable storage controls before operating it as a public service. In-memory jobs and rate limits are lost on restart. The included Render service uses an ephemeral filesystem; output files can disappear when the service restarts or sleeps.

## Requirements

- Python 3.10 or newer
- FFmpeg on `PATH` for stream merging and audio conversion

### Install FFmpeg

Windows (winget):

```powershell
winget install --id Gyan.FFmpeg.Essentials --exact
```

macOS (Homebrew):

```sh
brew install ffmpeg
```

Ubuntu or Debian:

```sh
sudo apt-get update
sudo apt-get install ffmpeg
```

Fedora:

```sh
sudo dnf install ffmpeg
```

## Run locally

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

macOS/Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000 to inspect a supported YouTube URL.

## Run on Android

This runs the app directly on an Android phone, so requests come from the phone's network instead of a cloud host. YouTube access can still vary by network, and this does not bypass YouTube access checks.

1. Install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) or the [official GitHub releases](https://github.com/termux/termux-app/releases). Do not use the outdated Play Store build.
2. Open Termux and run:

   ```sh
   pkg update
   pkg install git python ffmpeg
   git clone https://github.com/alentsiby/YtDown.git
   cd YtDown
   python -m venv .venv
   . .venv/bin/activate
   python -m pip install -r requirements.txt
   python app.py
   ```

3. Keep Termux open and visit http://127.0.0.1:5000 in the browser on the same phone.
4. Use **Save file** in the browser to save completed output. Downloads are stored inside Termux's app data; uninstalling Termux removes them.

The server listens only on the phone's loopback interface by default; do not change it to listen publicly for this setup. Android may pause background processes, so keep Termux open while inspecting or downloading.

For access from your phone when your computer cannot stay on, see the [private VPS setup guide](./VPS_SETUP.md).

## Tests

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use mocked source metadata; they do not fetch or redistribute sample media.

## Deploy on Render

The repository includes a Dockerfile and `render.yaml` Blueprint. Connect the repository in Render and create the Blueprint to build the app with FFmpeg. The service is public by default; keep the source repository private if desired, and add access controls before sharing the service URL. Review the host's acceptable-use rules before deployment. The free-tier filesystem is temporary, and this app is not configured for durable job storage.

## Project files

- `app.py`: Flask routes, validation, job tracking, cleanup, and yt-dlp integration.
- `templates/index.html`: responsive interface.
- `Dockerfile`, `render.yaml`: Render deployment configuration.
- `tests/test_app.py`: URL, metadata, validation, job-limit, and cleanup tests.