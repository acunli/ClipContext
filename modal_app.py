"""Modal deployment of the ClipContext FastAPI backend.

Deploy (from the repo root, after `modal token new` once):

    modal deploy modal_app.py        # or: make deploy-backend

Serves the exact same `src.api.app:app` that `make backend` and the
Dockerfile run, so there is no Modal-specific business logic anywhere else
in the codebase. Environment variables come from the Modal secret named
`clipcontext-backend` (see docs/Deployment.md for how to create it).

Why these settings:

- max_containers=1: the job registry (src/api/jobs.py) and the YouTube
  session/state/token stores (src/youtube/) are in-memory. A second
  container would split that state, so job polling and YouTube OAuth
  callbacks could land on a container that never saw the job/session.
  This is the Modal equivalent of the old railway.toml `numReplicas = 1`.
- @modal.concurrent: with only one container allowed, that container must
  serve concurrent requests itself (job polling while an upload streams
  in, health checks, etc.), exactly like a single uvicorn process does.
- scaledown_window=20 min (Modal's maximum): the pipeline runs in a
  background thread started by POST /api/jobs, and the container only
  stays alive while requests keep arriving. The frontend polls
  GET /api/jobs/{id} every few seconds during processing, which keeps it
  warm; the long window lets a job finish even if the tab is closed
  mid-run. When idle past that window, the container scales to zero and
  costs nothing — the next request cold-starts it (a few seconds).
- cpu/memory: far above the ~1GB Railway plan that faster-whisper used to
  OOM on (docs/Deployment.md "The real memory constraint").
"""

from pathlib import Path

import modal


PROJECT_ROOT = Path(__file__).parent

image = (
    modal.Image.debian_slim(python_version="3.13")
    # ffmpeg/ffprobe: audio extraction + video probing (src/video/).
    # libgl1/libglib2.0-0: shared libraries opencv-python needs at import
    # time on a slim Debian base.
    .apt_install("ffmpeg", "libgl1", "libglib2.0-0")
    # The lock file, not requirements.txt: requirements.txt is unpinned, and
    # a fresh resolve pulled a PyAV release that faster-whisper 1.2.1's
    # decode_audio() is incompatible with ("open() got an unexpected
    # keyword argument 'metadata_errors'"), failing every transcription.
    .pip_install_from_requirements(str(PROJECT_ROOT / "requirements-lock.txt"))
    # Bake the default faster-whisper model into the image so the first job
    # after a cold start doesn't wait on a model download. Must match
    # DEFAULT_WHISPER_MODEL_SIZE in src/ai/transcriber.py; if
    # WHISPER_MODEL_SIZE is overridden in the secret, that model is simply
    # downloaded on first use instead.
    .run_commands(
        "python -c \"from faster_whisper import download_model; download_model('tiny')\""
    )
    .env({"PYTHONUNBUFFERED": "1"})
    # Lands at /root/src; src/pipeline/paths.py derives PROJECT_ROOT from
    # its own file location, so runtime data/ and outputs/ live under /root
    # on the container's ephemeral disk.
    .add_local_dir(
        PROJECT_ROOT / "src",
        remote_path="/root/src",
        ignore=["**/__pycache__", "**/*.pyc"],
    )
)

app = modal.App("clipcontext-backend")


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("clipcontext-backend")],
    cpu=2.0,
    memory=4096,
    max_containers=1,
    scaledown_window=20 * 60,
    timeout=10 * 60,
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app(label="clipcontext-api")
def fastapi_app():
    # Imported inside the container: src.api.app validates required env
    # vars at import time, and those only exist once the secret is attached.
    from src.api.app import app as web_app

    return web_app
