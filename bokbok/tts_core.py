"""Shared Chatterbox Turbo synthesis logic used by read_aloud.py and app.py.

Speed notes:
- Voice-cloning conditioning (a full forward pass over the reference clip) is
  computed once per voice and cached (_conds_cache), instead of recomputing it
  on every chunk -- ~25-30% faster for multi-chunk text with a cloned voice.
- iter_speech() yields each chunk's audio as soon as it's ready (no waiting
  for the whole text), so a caller can start playback immediately.

Gotcha this module works around: ChatterboxTurboTTS keeps the "current" voice
conditioning in a single shared `model._conds` slot -- prepare_conditionals()
sets it as a side effect (it doesn't return the object), and generate() always
reads from that same slot (it has no `conds=` parameter). So _get_conds()
below must snapshot `model._conds` right after computing it, and iter_speech()
must restore the right snapshot onto `model._conds` before every generate()
call -- otherwise switching voices between requests can silently keep using
whichever voice was generated most recently.
"""

import json
import os
import queue
import re
import shutil
import tempfile
import threading
import wave
from pathlib import Path

MODEL = "mlx-community/chatterbox-turbo-fp16"
ROOT = Path(__file__).parent
VOICES_FILE = ROOT / "voices.json"
EXAGGERATION = 0.5  # Turbo ignores it; passed only to satisfy prepare_conditionals' signature

# Per-chunk word budget. The model splits long text internally too (~800
# tokens per piece), but chunking ourselves first is what gives each chunk a
# clean boundary to stream as its own frame / playback unit. The *first* chunk
# is kept much smaller so the listener hears something within a second or two
# instead of waiting for a full 50-word chunk to generate.
CHUNK_WORD_BUDGET = 50
FIRST_CHUNK_WORD_BUDGET = 15

# For the streaming path, the model's stream_generate() hands back audio every
# STREAM_TOKEN_CHUNK speech tokens (~1.6s at 25 tok/s) *within* a chunk, so the
# first sound lands in ~0.3s instead of after a whole chunk (~2-3s). It costs
# ~1.5x the compute of a plain generate() (it re-vocodes the cumulative token
# sequence each time) but still runs ~3-4x faster than real-time, so the
# listener never waits. Batch callers (synthesize) keep the cheaper generate().
STREAM_TOKEN_CHUNK = 40

_model = None
_conds_cache: dict = {}  # voice name -> (fingerprint, Conditionals); see _get_conds
_builtin_conds = None  # the model's own default conditioning, snapshotted at load time
_lock = threading.Lock()  # serializes all model access: MLX generate() isn't safe for concurrent calls


def load_voices() -> dict:
    if not VOICES_FILE.exists():
        return {"default": {"ref_audio": None, "ref_text": None, "description": "Model's built-in voice"}}
    voices = json.loads(VOICES_FILE.read_text())
    for preset in voices.values():
        ref_audio = preset.get("ref_audio")
        if ref_audio and not Path(ref_audio).is_absolute():
            preset["ref_audio"] = str(ROOT / ref_audio)
    return voices


def split_into_chunks(
    text: str, word_budget: int = CHUNK_WORD_BUDGET, first_budget: int = FIRST_CHUNK_WORD_BUDGET
) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []

    def budget() -> int:  # smaller budget until the first chunk is out the door
        return first_budget if not chunks else word_budget

    for para in paragraphs:
        sentences = re.split(r"(?<=[.!?])\s+", para)
        current: list[str] = []
        current_words = 0
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            words = sentence.split()
            # A "sentence" bigger than the whole budget (sparse/no punctuation
            # for a long stretch) must still be hard-split -- otherwise it
            # becomes one unbounded chunk that stalls streaming and risks
            # hitting the model's own generation-length cap.
            if len(words) > budget():
                if current:
                    chunks.append(" ".join(current))
                    current, current_words = [], 0
                i = 0
                while i < len(words):
                    n = budget()
                    chunks.append(" ".join(words[i : i + n]))
                    i += n
                continue
            if current and current_words + len(words) > budget():
                chunks.append(" ".join(current))
                current, current_words = [], 0
            current.append(sentence)
            current_words += len(words)
        if current:
            chunks.append(" ".join(current))
    return chunks


def concatenate_wavs(chunk_paths: list[Path], output_path: Path) -> None:
    with wave.open(str(chunk_paths[0]), "rb") as first:
        params = first.getparams()

    with wave.open(str(output_path), "wb") as out:
        out.setparams(params)
        for path in chunk_paths:
            with wave.open(str(path), "rb") as chunk:
                out.writeframes(chunk.readframes(chunk.getnframes()))


def get_model():
    global _model, _builtin_conds
    if _model is None:
        from mlx_audio.tts.utils import load_model

        _model = load_model(model_path=MODEL)
        # Snapshot the model's own built-in conditioning now, before any
        # custom voice's prepare_conditionals() call overwrites it (see below).
        _builtin_conds = _model._conds
    return _model


def _get_conds(model, voice: str, preset: dict):
    """Voice-cloning conditioning, computed once per voice and cached for the
    life of the process (must hold _lock when calling).

    IMPORTANT: ChatterboxTurboTTS.prepare_conditionals() does NOT return the
    Conditionals object -- it sets it as a side effect on the single shared
    `model._conds` slot and returns None. generate() also has no `conds=`
    parameter; it always reads from that same shared slot. So caching by
    voice name isn't enough on its own -- iter_speech() must also restore
    `model._conds` to the right cached value before every generate() call,
    or a request can silently end up using whichever voice was used last.
    """
    ref_audio = preset.get("ref_audio")
    # The cache is keyed on the *file*, not just the name: if voices.json is
    # edited to repoint a name, or a voice is deleted and re-added under the
    # same key, the fingerprint changes and we recompute instead of serving
    # stale conditioning. No explicit invalidation needed anywhere.
    fingerprint = (ref_audio, os.stat(ref_audio).st_mtime_ns) if ref_audio else None
    cached = _conds_cache.get(voice)
    if cached is None or cached[0] != fingerprint:
        if ref_audio:
            model.prepare_conditionals(ref_audio, sample_rate=model.sample_rate, exaggeration=EXAGGERATION)
            conds = model._conds
        else:
            conds = _builtin_conds
        _conds_cache[voice] = (fingerprint, conds)
    return _conds_cache[voice][1]


def _wav_bytes(audio, sample_rate: int) -> bytes:
    import io

    from mlx_audio.audio_io import write as audio_write

    buf = io.BytesIO()
    audio_write(buf, audio, sample_rate, format="wav")
    return buf.getvalue()


def iter_speech(text: str, voice: str = "default", word_budget: int = CHUNK_WORD_BUDGET, stream: bool = True):
    """Yield raw WAV bytes as speech is generated, so a caller can start
    playback long before the rest of the text is done.

    stream=True  -> pieces of ~1.6s arrive continuously *within* each chunk
                    (first sound in ~0.3s). Use for live playback.
    stream=False -> one piece per text chunk, ~1.5x less compute. Use when
                    nobody is listening until the whole thing is done.
    """
    voices = load_voices()
    if voice not in voices:
        raise ValueError(f"Unknown voice '{voice}'. Available: {', '.join(sorted(voices))}")
    preset = voices[voice]

    chunks = split_into_chunks(text, word_budget)
    if not chunks:
        raise ValueError("No text to synthesize")

    import mlx.core as mx

    model = get_model()
    sr = model.sample_rate

    for chunk in chunks:
        if not stream:
            # Lock is held only around the model call, never across a `yield`
            # -- if the consumer (e.g. a disconnected HTTP client) never
            # resumes us, the lock must still be released.
            with _lock:
                model._conds = _get_conds(model, voice, preset)  # restore the shared slot for this voice
                results = list(model.generate(text=chunk, verbose=False))
                wav = results[0].audio if len(results) == 1 else mx.concatenate([r.audio for r in results], axis=0)
                data = _wav_bytes(wav, sr)
            yield data
            continue

        # Streaming: stream_generate() keeps model state live between pieces,
        # so the lock has to span the whole chunk. To avoid holding it across
        # our own `yield`s (the deadlock trap above), a producer thread owns the
        # lock and drops finished pieces into a queue; it always runs the chunk
        # to completion, so an abandoned consumer wastes at most one chunk and
        # can never wedge the lock.
        q: queue.Queue = queue.Queue()

        def produce(chunk=chunk):
            try:
                with _lock:
                    model._conds = _get_conds(model, voice, preset)
                    for r in model.stream_generate(
                        text=chunk, chunk_size=STREAM_TOKEN_CHUNK, cfg_weight=0.0, exaggeration=0.0, verbose=False
                    ):
                        q.put(_wav_bytes(r.audio, sr))
            except BaseException as e:  # hand the failure to the consumer
                q.put(e)
            finally:
                q.put(None)

        threading.Thread(target=produce, daemon=True).start()
        while (item := q.get()) is not None:
            if isinstance(item, BaseException):
                raise item
            yield item


def synthesize(text: str, voice: str = "default", word_budget: int = CHUNK_WORD_BUDGET) -> Path:
    """Generate speech and return a path to one concatenated wav file
    (batch mode: waits for everything). Caller must delete path.parent."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="tts_"))
    try:
        chunk_paths = []
        for i, data in enumerate(iter_speech(text, voice, word_budget, stream=False)):
            p = tmp_dir / f"part_{i:04d}.wav"
            p.write_bytes(data)
            chunk_paths.append(p)
        output_path = tmp_dir / "final.wav"
        concatenate_wavs(chunk_paths, output_path)
        return output_path
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def warm_up() -> None:
    """Run one throwaway generation so the first real request isn't slowed by
    MLX's one-time kernel compilation. Deliberately lets any failure propagate:
    if the model can't load, better to die at startup than to print "Ready."
    and then fail every request."""
    for _ in iter_speech("Hello there.", "default", stream=False):
        pass
    for _ in iter_speech("Hello there.", "default", stream=True):  # compiles the streaming kernels too
        pass


def prewarm_voices() -> None:
    """Compute conditioning for every voice in voices.json so the first pick of
    each one is instant. Meant to run in a background thread after startup:
    each voice takes _lock briefly, so a real request that arrives mid-way just
    waits for the current voice to finish (~1s) rather than for all of them."""
    import time

    model = get_model()
    voices = load_voices()
    ok = 0
    for name, preset in voices.items():
        try:
            with _lock:
                _get_conds(model, name, preset)
            ok += 1
        except Exception as e:  # one bad clip shouldn't stop the rest
            print(f"prewarm: skipping voice '{name}': {e}", flush=True)
        # threading.Lock isn't fair: re-acquiring immediately in a tight loop
        # can starve a real request for the whole pre-warm (~20s). A short
        # pause hands the lock to anyone waiting.
        time.sleep(0.1)
    print(f"prewarm: {ok}/{len(voices)} voices ready.", flush=True)
