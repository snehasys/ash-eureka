#!/usr/bin/env python3
"""Tiny local REST API for Chatterbox Turbo, reachable by anyone on the same Wi-Fi.

    .venv/bin/python app.py

Then open the printed URL (or http://<this-machine>.local:5050/) from any
device on the same network.
"""

import io
import json
import logging
import os
import re
import shutil
import socket
import struct
import threading
import wave
from pathlib import Path

# Quiet the noise so the log is just what this app is doing (see tts_core.log):
# no per-token progress bars from the models, no HF "model type" warnings, and
# no per-request access lines from the dev server (errors still show).
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")  # "PyTorch was not found" banner etc.
os.environ.setdefault("HF_HUB_VERBOSITY", "error")  # "unauthenticated requests" nag
logging.getLogger("werkzeug").setLevel(logging.WARNING)
try:
    from transformers.utils import logging as _hf_logging

    _hf_logging.set_verbosity_error()
except Exception:
    pass

from flask import Flask, Response, jsonify, request, send_file

from tts_core import (
    VOICES_FILE,
    iter_speech,
    load_voices,
    log,
    omni_is_cached,
    omni_is_loaded,
    prewarm_voices,
    synthesize,
    warm_up,
)

# Frames on /speak/stream are <4-byte big-endian length><wav bytes>. This
# length value instead marks an error frame: <ERROR_FRAME><4-byte len><utf-8 message>,
# so a failure mid-stream reaches the browser as a message rather than silence.
ERROR_FRAME = 0xFFFFFFFF

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # short voice clips only; also caps upload DoS

# Generous on purpose -- long texts are a real use case (drop a whole book
# chapter or more). 300k chars is roughly 6-7 hours of speech; streaming means
# the listener starts hearing it in under a second regardless of length. The
# cap just stops a stray paste from tying up the shared model indefinitely.
# Keep in sync with MAX_TEXT_CHARS in index.html.
MAX_TEXT_CHARS = 300_000

RECORDED_DIR = Path(__file__).parent / "voices" / "recorded"  # project-local; never the symlinked dir
_voices_lock = threading.Lock()  # guards voices.json read-modify-write now that the server is threaded


def _text_error(text: str):
    if not text:
        return "text is required"
    if len(text) > MAX_TEXT_CHARS:
        return f"Text is too long ({len(text):,} chars; max {MAX_TEXT_CHARS:,})."
    return None


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error="Audio file is too large (max 25MB)."), 413


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "voice"

PAGE_FILE = Path(__file__).parent / "index.html"


def _device(ua: str) -> str:
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                         ("Windows", "Windows"), ("Linux", "Linux"), ("curl", "curl")):
        if needle in ua:
            return name
    return "unknown device"


@app.get("/")
def index():
    log(f"page opened from {request.remote_addr} ({_device(request.user_agent.string)})")
    # Read per request so edits to index.html show up on refresh without a restart.
    return PAGE_FILE.read_text()


@app.get("/voices")
def voices():
    return jsonify({name: p.get("description", "") for name, p in load_voices().items()})


@app.get("/status")
def status():
    # Lets the page warn that the first Bengali/Hindi request will take a
    # while (engine still loading, or not even downloaded yet).
    return jsonify(omni_loaded=omni_is_loaded(), omni_cached=omni_is_cached())


@app.post("/voices")
def add_voice():
    """Add a new voice from an uploaded clip. The client always converts
    recordings/uploads to WAV in the browser first (see blobToWavBlob in the
    page's JS), so ffmpeg is never needed here -- only .wav ever arrives.
    """
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or name
    audio = request.files.get("audio")

    if not name:
        return jsonify(error="Name is required."), 400
    if len(name) > 60:
        return jsonify(error="Name is too long (max 60 characters)."), 400
    if audio is None or audio.filename == "":
        return jsonify(error="An audio file is required."), 400

    data = audio.read()
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        return jsonify(error="Audio must be a WAV file."), 400
    try:
        with wave.open(io.BytesIO(data)) as w:
            duration = w.getnframes() / w.getframerate()
    except wave.Error:
        return jsonify(error="Could not read this audio file -- is it a valid WAV?"), 400
    if duration <= 5.0:
        return jsonify(error=f"Recording is only {duration:.1f}s -- needs to be over 5 seconds."), 400

    with _voices_lock:
        voices_raw = json.loads(VOICES_FILE.read_text()) if VOICES_FILE.exists() else {}

        RECORDED_DIR.mkdir(parents=True, exist_ok=True)
        existing_files = {p.name for p in RECORDED_DIR.glob("*.wav")}
        base_slug = _slugify(name)
        slug, key, i = base_slug, f"custom_{base_slug}", 2
        while key in voices_raw or f"{slug}.wav" in existing_files:
            slug = f"{base_slug}_{i}"
            key = f"custom_{slug}"
            i += 1

        dest = RECORDED_DIR / f"{slug}.wav"
        dest.write_bytes(data)

        voices_raw[key] = {
            "ref_audio": f"voices/recorded/{dest.name}",
            "ref_text": None,
            "description": description,
        }
        VOICES_FILE.write_text(json.dumps(voices_raw, indent=2))
    log(f"voice added: '{key}' ({duration:.1f}s clip) from {request.remote_addr}")
    return jsonify(name=key, description=description), 201


@app.delete("/voices/<name>")
def delete_voice(name):
    with _voices_lock:
        voices_raw = json.loads(VOICES_FILE.read_text()) if VOICES_FILE.exists() else {}
        if name not in voices_raw:
            return jsonify(error="Voice not found."), 404
        if not name.startswith("custom_"):
            return jsonify(error="Only voices you've added can be deleted."), 403

        entry = voices_raw.pop(name)
        VOICES_FILE.write_text(json.dumps(voices_raw, indent=2))

    ref_audio = entry.get("ref_audio")
    if ref_audio:
        abs_path = Path(ref_audio)
        if not abs_path.is_absolute():
            abs_path = Path(__file__).parent / ref_audio
        # Never delete outside voices/recorded/ -- guards against voices.json
        # ever pointing a "custom_" key at the symlinked curated directory.
        if abs_path.resolve().is_relative_to(RECORDED_DIR.resolve()):
            abs_path.unlink(missing_ok=True)

    log(f"voice deleted: '{name}' from {request.remote_addr}")
    return jsonify(deleted=name)


@app.post("/speak")
def speak():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    voice = data.get("voice", "default")
    if err := _text_error(text):
        return jsonify(error=err), 400

    try:
        wav_path = synthesize(text, voice)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    response = send_file(wav_path, mimetype="audio/wav")
    response.call_on_close(lambda: shutil.rmtree(wav_path.parent, ignore_errors=True))
    return response


@app.post("/speak/stream")
def speak_stream():
    """Like /speak, but yields each generated chunk as soon as it's ready
    instead of waiting for the whole text -- lets the client start playing
    before generation is done. Each chunk is a length-prefixed WAV blob:
    4-byte big-endian length, then that many bytes of a self-contained .wav.
    """
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    voice = data.get("voice", "default")
    if err := _text_error(text):
        return jsonify(error=err), 400
    if voice not in load_voices():
        return jsonify(error=f"Unknown voice '{voice}'"), 400

    client = request.remote_addr  # read now: the generator below runs after the request context is gone

    def framed():
        try:
            for wav_bytes in iter_speech(text, voice, client=client):
                yield struct.pack(">I", len(wav_bytes)) + wav_bytes
        except Exception as e:  # headers are long gone; report in-band instead of going silent
            log(f"✗ request from {client} failed -- {type(e).__name__}: {e}")
            msg = f"{type(e).__name__}: {e}".encode()
            yield struct.pack(">I", ERROR_FRAME) + struct.pack(">I", len(msg)) + msg

    response = Response(framed(), mimetype="application/octet-stream")
    # Without this, Werkzeug auto-computes Content-Length by fully draining
    # the generator first, which would silently defeat streaming entirely.
    response.direct_passthrough = True
    return response


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet actually sent; just picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    # macOS's AirPlay Receiver squats on port 5000, so default to something else.
    port = int(os.environ.get("PORT", 5050))
    hostname = socket.gethostname()
    print(f"Local:  http://127.0.0.1:{port}/", flush=True)
    print(f"Wi-Fi:  http://{lan_ip()}:{port}/", flush=True)
    print(f"Bonjour: http://{hostname}:{port}/  (works on most Macs/iPhones without typing an IP)", flush=True)
    print("Warming up the model (one-time MLX compile)...", flush=True)
    warm_up()
    print("Ready.", flush=True)
    # Pre-compute every voice's conditioning in the background so the first
    # pick of each is instant; the server is already accepting requests.
    threading.Thread(target=prewarm_voices, daemon=True).start()
    # threaded=True: page loads, /voices and uploads stay responsive while
    # someone's long generation runs. Model access is still serialized by
    # tts_core._lock, so two generations interleave chunk-by-chunk.
    app.run(host="0.0.0.0", port=port, threaded=True)
