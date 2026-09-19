#!/usr/bin/env python3
"""Tiny local REST API for Chatterbox Turbo, reachable by anyone on the same Wi-Fi.

    .venv/bin/python app.py

Then open the printed URL (or http://<this-machine>.local:5050/) from any
device on the same network.
"""

import os
import shutil
import socket
import struct

from flask import Flask, Response, jsonify, request, send_file

from tts_core import iter_speech, load_voices, synthesize, warm_up

app = Flask(__name__)

PAGE = """<!doctype html>
<title>ASH_Chat_bokbok</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg1: #eef2ff; --bg2: #fdf4ff;
    --card: #ffffffdd; --text: #1e1b2e; --muted: #6b6478;
    --accent1: #7c5cff; --accent2: #ff6fb0; --danger: #e5484d;
    --border: #e6e1f5; --radius: 16px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg1: #14101f; --bg2: #1c1330;
      --card: #221a35dd; --text: #f1eefc; --muted: #a79bc4;
      --border: #34294f; --danger: #ff6369;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    margin: 0; min-height: 100vh; color: var(--text);
    display: flex; align-items: center; justify-content: center; padding: 1.5rem;
    background: radial-gradient(circle at 20% 10%, var(--bg1), var(--bg2));
  }
  .card {
    width: 100%; max-width: 30rem; background: var(--card);
    border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: 0 20px 40px -20px rgba(80, 40, 140, 0.35);
    padding: 1.75rem; backdrop-filter: blur(6px);
  }
  h1 {
    margin: 0 0 0.15rem; font-size: 1.4rem;
    background: linear-gradient(90deg, var(--accent1), var(--accent2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .sub { margin: 0 0 1.25rem; color: var(--muted); font-size: 0.85rem; }
  label { display: block; font-size: 0.75rem; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.04em; margin: 0 0 0.35rem; }
  select, textarea {
    width: 100%; font-size: 0.95rem; font-family: inherit; color: var(--text);
    background: var(--bg1); border: 1px solid var(--border); border-radius: 10px;
    padding: 0.6rem 0.7rem; margin-bottom: 1rem;
  }
  @media (prefers-color-scheme: dark) { select, textarea { background: #1a1428; } }
  textarea { height: 6rem; resize: vertical; }
  .tags { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: -0.6rem 0 1rem; }
  .tag {
    font-size: 0.75rem; padding: 0.25rem 0.6rem; border-radius: 999px;
    border: 1px solid var(--border); background: transparent; color: var(--muted); cursor: pointer;
  }
  .tag:hover { border-color: var(--accent1); color: var(--accent1); }
  button#go {
    width: 100%; font-size: 1rem; font-weight: 600; color: white; cursor: pointer;
    padding: 0.7rem; border: none; border-radius: 10px;
    background: linear-gradient(90deg, var(--accent1), var(--accent2));
    box-shadow: 0 8px 20px -8px rgba(124, 92, 255, 0.6);
    transition: transform 0.1s ease, opacity 0.2s ease;
  }
  button#go:active { transform: scale(0.98); }
  button#go:disabled { opacity: 0.6; cursor: default; }
  button#replay {
    font-size: 1.1rem; border-radius: 10px; border: 1px solid var(--border);
    background: transparent; color: var(--accent1);
  }
  button#replay:hover { background: var(--bg1); }
  button#pause {
    font-size: 1rem; border-radius: 10px; border: 1px solid var(--border);
    background: transparent; color: var(--accent1);
  }
  button#pause:hover { background: var(--bg1); }
  button#clear {
    font-size: 1rem; border-radius: 10px; border: 1px solid var(--border);
    background: transparent; color: var(--danger);
  }
  button#clear:hover { background: var(--bg1); border-color: var(--danger); }
  select#speed { width: auto; margin-bottom: 0; padding: 0.6rem 0.5rem; flex: 0 0 auto; }
  .status { min-height: 1.4rem; margin-top: 0.6rem; font-size: 0.85rem; color: var(--muted); }
  .spinner {
    display: inline-block; width: 0.8rem; height: 0.8rem; margin-right: 0.4rem;
    border: 2px solid var(--border); border-top-color: var(--accent1);
    border-radius: 50%; vertical-align: -1px; animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
<div class="card">
  <h1>ASH_Chat_bokbok</h1>
  <p class="sub">Pick a voice, type something, hear it on this device.</p>

  <label for="voice">Voice</label>
  <select id="voice"></select>

  <label for="text">Text</label>
  <textarea id="text" placeholder="Hi there! [chuckle] how's it going?"></textarea>
  <div class="tags">
    <button class="tag" type="button" data-tag="chuckle">chuckle</button>
    <button class="tag" type="button" data-tag="laugh">laugh</button>
    <button class="tag" type="button" data-tag="sigh">sigh</button>
    <button class="tag" type="button" data-tag="gasp">gasp</button>
    <button class="tag" type="button" data-tag="cough">cough</button>
    <button class="tag" type="button" data-tag="groan">groan</button>
  </div>

  <div style="display:flex; gap:0.5rem;">
    <select id="speed" title="Playback speed">
      <option value="0.75">0.75x</option>
      <option value="1" selected>1x</option>
      <option value="1.25">1.25x</option>
      <option value="1.5">1.5x</option>
      <option value="2">2x</option>
    </select>
    <button id="go" style="flex:1">Speak</button>
    <button id="pause" style="display:none; flex:0 0 auto; width:auto; padding:0 1rem;" title="Pause/resume">&#10074;&#10074;</button>
    <button id="replay" style="display:none; flex:0 0 auto; width:auto; padding:0 1rem;" title="Replay without regenerating">&#8635;</button>
    <button id="clear" style="flex:0 0 auto; width:auto; padding:0 1rem;" title="Clear everything (keeps the text box) and discard the loaded audio">&#128465;&#65039;</button>
  </div>
  <div class="status" id="status"></div>
</div>
<script>
const GROUPS = [
  ["uk_", "British / Irish"], ["us_", "American"], ["hindi_", "Hindi"],
];

fetch("/voices").then(r => r.json()).then(voices => {
  const sel = document.getElementById("voice");
  const groups = {};
  for (const [name, desc] of Object.entries(voices)) {
    const hit = GROUPS.find(([prefix]) => name.startsWith(prefix));
    const label = hit ? hit[1] : "Built-in";
    (groups[label] ??= []).push([name, desc]);
  }
  for (const [label, entries] of Object.entries(groups)) {
    const og = document.createElement("optgroup");
    og.label = label;
    for (const [name, desc] of entries) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = desc || name;
      og.appendChild(opt);
    }
    sel.appendChild(og);
  }
  // Replay reuses the *previous* generation's audio verbatim -- if you've
  // since picked a different voice, replaying would silently play the old
  // one back, which looks exactly like "switching voice didn't work".
  sel.onchange = () => {
    if (sel.value !== lastVoice) replayBtn.style.display = "none";
  };
});

const textEl = document.getElementById("text");
document.querySelectorAll(".tag").forEach(btn => {
  btn.onclick = () => {
    const insert = `[${btn.dataset.tag}] `;
    const pos = textEl.selectionStart ?? textEl.value.length;
    textEl.value = textEl.value.slice(0, pos) + insert + textEl.value.slice(pos);
    textEl.focus();
    textEl.selectionStart = textEl.selectionEnd = pos + insert.length;
  };
});

const goBtn = document.getElementById("go");
const replayBtn = document.getElementById("replay");
const pauseBtn = document.getElementById("pause");
const clearBtn = document.getElementById("clear");
const speedSel = document.getElementById("speed");
const status = document.getElementById("status");

const ctx = new (window.AudioContext || window.webkitAudioContext)();
let lastBuffers = [];      // decoded AudioBuffers from the last run, for instant replay
let lastVoice = null;      // which voice those buffers were generated with
let activeSources = [];    // currently scheduled/playing nodes, so speed changes apply live
let speed = 1;
let inFlight = null;       // AbortController for the request currently streaming in, if any

function stopPlayback() {
  activeSources.forEach(src => { try { src.stop(); } catch {} });
  activeSources = [];
}

speedSel.onchange = () => {
  speed = parseFloat(speedSel.value);
  activeSources.forEach(src => { try { src.playbackRate.value = speed; } catch {} });
};

pauseBtn.onclick = async () => {
  if (ctx.state === "running") {
    await ctx.suspend();
    pauseBtn.textContent = "▶";
  } else {
    await ctx.resume();
    pauseBtn.textContent = "❚❚";
  }
};

function playAt(buf, startAt) {
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.playbackRate.value = speed;
  src.connect(ctx.destination);
  src.start(startAt);
  activeSources.push(src);
  src.onended = () => { activeSources = activeSources.filter(s => s !== src); };
  return src;
}

function schedule(buffers) {
  let t = ctx.currentTime;
  for (const buf of buffers) {
    playAt(buf, t);
    t += buf.duration / speed;
  }
}

goBtn.onclick = async () => {
  const text = textEl.value.trim();
  const voice = document.getElementById("voice").value;
  if (!text) { status.textContent = "Type something first."; return; }

  goBtn.disabled = true;
  replayBtn.style.display = "none";
  pauseBtn.style.display = "block";
  pauseBtn.textContent = "❚❚";
  status.innerHTML = '<span class="spinner"></span>Generating...';
  lastBuffers = [];
  activeSources = [];
  let nextStartTime = 0;
  let chunkCount = 0;

  const abortController = new AbortController();
  inFlight = abortController;

  try {
    if (ctx.state === "suspended") await ctx.resume();
    const resp = await fetch("/speak/stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text, voice}),
      signal: abortController.signal,
    });
    if (!resp.ok) throw new Error((await resp.json()).error || "Something went wrong");

    const reader = resp.body.getReader();
    let buffered = new Uint8Array(0);

    const append = (a, b) => {
      const merged = new Uint8Array(a.length + b.length);
      merged.set(a, 0);
      merged.set(b, a.length);
      return merged;
    };

    while (true) {
      const { done, value } = await reader.read();
      if (value) buffered = append(buffered, value);

      while (buffered.length >= 4) {
        const len = new DataView(buffered.buffer, buffered.byteOffset, 4).getUint32(0, false);
        if (buffered.length < 4 + len) break;
        const wavBytes = buffered.slice(4, 4 + len);
        buffered = buffered.slice(4 + len);

        const audioBuf = await ctx.decodeAudioData(wavBytes.buffer);
        lastBuffers.push(audioBuf);
        const startAt = Math.max(ctx.currentTime, nextStartTime);
        playAt(audioBuf, startAt);
        nextStartTime = startAt + audioBuf.duration / speed;

        chunkCount += 1;
        status.textContent = `Playing... (part ${chunkCount})`;
      }
      if (done) break;
    }
    status.textContent = "";
    lastVoice = voice;
    replayBtn.style.display = lastBuffers.length ? "block" : "none";
  } catch (err) {
    if (err.name !== "AbortError") status.textContent = "Error: " + err.message;
  } finally {
    inFlight = null;
    goBtn.disabled = false;
  }
};

replayBtn.onclick = async () => {
  if (ctx.state === "suspended") await ctx.resume();
  stopPlayback();
  pauseBtn.style.display = "block";
  pauseBtn.textContent = "❚❚";
  schedule(lastBuffers);
};

clearBtn.onclick = () => {
  if (inFlight) inFlight.abort();   // cancel any in-progress generation
  stopPlayback();                   // stop anything currently playing/queued
  lastBuffers = [];                 // discard the downloaded/decoded audio
  lastVoice = null;
  speed = 1;
  speedSel.value = "1";
  const voiceSel = document.getElementById("voice");
  if (voiceSel.options.length) voiceSel.selectedIndex = 0;
  pauseBtn.style.display = "none";
  pauseBtn.textContent = "❚❚";
  replayBtn.style.display = "none";
  status.textContent = "";
  goBtn.disabled = false;
};
</script>
"""


@app.get("/")
def index():
    return PAGE


@app.get("/voices")
def voices():
    return jsonify({name: p.get("description", "") for name, p in load_voices().items()})


@app.post("/speak")
def speak():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    voice = data.get("voice", "default")
    if not text:
        return jsonify(error="text is required"), 400

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
    if not text:
        return jsonify(error="text is required"), 400
    if voice not in load_voices():
        return jsonify(error=f"Unknown voice '{voice}'"), 400

    def framed():
        for wav_bytes in iter_speech(text, voice):
            yield struct.pack(">I", len(wav_bytes)) + wav_bytes

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
    print(f"Local:  http://127.0.0.1:{port}/")
    print(f"Wi-Fi:  http://{lan_ip()}:{port}/")
    print(f"Bonjour: http://{hostname}:{port}/  (works on most Macs/iPhones without typing an IP)")
    print("Warming up the model (one-time MLX compile)...")
    warm_up()
    print("Ready.")
    app.run(host="0.0.0.0", port=port)
