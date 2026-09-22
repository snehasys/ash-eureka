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
import time
import warnings
import wave
from pathlib import Path

# OmniVoice's codec builds a sinc kernel with a harmless 0/0 at the centre tap
# (immediately overwritten by np.where) and numpy warns about it on every load.
warnings.filterwarnings("ignore", category=RuntimeWarning, module=r"mlx_audio\.codec")


def log(msg: str) -> None:
    """One line per meaningful event, timestamped, flushed (the server's stdout
    is usually a file or a terminal someone is watching for progress)."""
    print(time.strftime("%H:%M:%S"), msg, flush=True)

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

# Chatterbox Turbo is English-only: its GPT-2 byte-level vocab shatters other
# scripts into junk tokens that come out as noise. Chunks written in the scripts
# below are routed to OmniVoice instead, which reads 646 languages and clones
# the selected voice cross-lingually. Tags are BCP-47, as OmniVoice's README uses.
OMNI_MODEL = "mlx-community/OmniVoice-bfloat16"
OMNI_NUM_STEPS = 32  # diffusion steps: 16 is faster, 32 is cleaner
OMNI_REF_MAX_S = 10.0  # OmniVoice clones from at most this much of the reference clip
# OmniVoice can't stream within a chunk -- each chunk arrives as one piece --
# so its chunks are kept shorter than Chatterbox's (~10s of audio, not ~22s)
# to keep playback arriving continuously and per-chunk latency low.
OMNI_WORD_BUDGET = 25
SCRIPT_LANGS = [((0x0980, 0x09FF), "bn"), ((0x0900, 0x097F), "hi")]  # Bengali, Devanagari

SAMPLE_RATE = 24000  # both engines output 24kHz

# Only ONE engine is resident at a time: Chatterbox is ~2.8GB, OmniVoice ~1.8GB,
# both together ~4.7GB. Swapping is cheap (~0.5s when the weights are in the OS
# file cache, ~30s cold), and the per-voice conditioning caches survive a swap,
# so a request that needs the other engine just triggers a swap. Set
# TTS_DEFAULT_ENGINE=omnivoice to start with OmniVoice instead of Chatterbox.
ENGINES = ("chatterbox", "omnivoice")
DEFAULT_ENGINE = os.environ.get("TTS_DEFAULT_ENGINE", "chatterbox")
if DEFAULT_ENGINE not in ENGINES:
    DEFAULT_ENGINE = "chatterbox"

_model = None  # resident Chatterbox, or None
_omni_model = None  # resident OmniVoice, or None
_omni_ref_cache: dict = {}  # voice name -> (fingerprint, OmniVoice ref_tokens)
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

    def budget(lang) -> int:  # smaller budget until the first chunk is out the door
        if not chunks:
            return first_budget
        return min(word_budget, OMNI_WORD_BUDGET) if lang else word_budget

    for para in paragraphs:
        sentences = re.split(r"(?<=[.!?।॥])\s+", para)  # । ॥ = Bengali/Devanagari full stops
        current: list[str] = []
        current_words = 0
        current_lang = None
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            # Never merge sentences of different scripts into one chunk: each
            # chunk is routed to one engine (Chatterbox vs OmniVoice) as a whole,
            # and a Bengali sentence buried among English ones would otherwise
            # be outvoted and come out as noise.
            lang = _script_language(sentence)
            if current and lang != current_lang:
                chunks.append(" ".join(current))
                current, current_words = [], 0
            current_lang = lang
            words = sentence.split()
            # A "sentence" bigger than the whole budget (sparse/no punctuation
            # for a long stretch) must still be hard-split -- otherwise it
            # becomes one unbounded chunk that stalls streaming and risks
            # hitting the model's own generation-length cap.
            if len(words) > budget(lang):
                if current:
                    chunks.append(" ".join(current))
                    current, current_words = [], 0
                i = 0
                while i < len(words):
                    n = budget(lang)
                    chunks.append(" ".join(words[i : i + n]))
                    i += n
                continue
            if current and current_words + len(words) > budget(lang):
                chunks.append(" ".join(current))
                current, current_words = [], 0
            current.append(sentence)
            current_words += len(words)
        if current:
            chunks.append(" ".join(current))
    return chunks


def _script_language(text: str):
    """BCP-47 tag of the non-Latin script that dominates `text`, or None for
    Chatterbox. A sentence with a few English loanwords still routes by its
    main script."""
    latin = sum(c.isascii() and c.isalpha() for c in text)
    best = None
    for (lo, hi), tag in SCRIPT_LANGS:
        n = sum(lo <= ord(c) <= hi for c in text)
        if n > latin and (best is None or n > best[0]):
            best = (n, tag)
    return best[1] if best else None


def concatenate_wavs(chunk_paths: list[Path], output_path: Path) -> None:
    with wave.open(str(chunk_paths[0]), "rb") as first:
        params = first.getparams()

    with wave.open(str(output_path), "wb") as out:
        out.setparams(params)
        for path in chunk_paths:
            with wave.open(str(path), "rb") as chunk:
                out.writeframes(chunk.readframes(chunk.getnframes()))


def engine_loaded():
    """Name of the resident engine, or None."""
    return "chatterbox" if _model is not None else "omnivoice" if _omni_model is not None else None


def active_memory_gb() -> float:
    import mlx.core as mx

    return mx.get_active_memory() / 2**30


def _unload(name: str) -> None:
    """Drop an engine and give its memory back (must hold _lock)."""
    global _model, _omni_model
    import gc

    import mlx.core as mx

    before = active_memory_gb()
    if name == "chatterbox":
        _model = None
    else:
        _omni_model = None
    gc.collect()
    mx.clear_cache()
    log(f"unloaded {name}: {before:.1f} GB -> {active_memory_gb():.1f} GB")


def get_engine(name: str):
    """The resident model for `name`, loading it -- and unloading the other
    engine first -- if needed. Must hold _lock: nothing may be generating
    while an engine is swapped out from under it."""
    global _model, _omni_model, _builtin_conds
    from mlx_audio.tts.utils import load_model

    if name == "chatterbox":
        if _model is None:
            if _omni_model is not None:
                _unload("omnivoice")
            t = time.time()
            _model = load_model(model_path=MODEL)
            # Snapshot the model's own built-in conditioning now, before any
            # custom voice's prepare_conditionals() call overwrites it.
            _builtin_conds = _model._conds
            log(f"Chatterbox Turbo loaded in {time.time() - t:.1f}s ({active_memory_gb():.1f} GB resident)")
            try:  # mlx-audio prints "S3 Token -> Mel Inference..." on every call, unconditionally
                from mlx_audio.tts.models.chatterbox_turbo.models.s3gen import flow_matching

                flow_matching.print = lambda *a, **k: None
            except Exception:
                pass
        return _model

    if _omni_model is None:
        if _model is not None:
            _unload("chatterbox")
        log("loading OmniVoice (multilingual engine)" + ("" if omni_is_cached() else " -- downloading ~3GB first, one time only"))
        t = time.time()
        _omni_model = load_model(model_path=OMNI_MODEL)
        log(f"OmniVoice loaded in {time.time() - t:.1f}s ({active_memory_gb():.1f} GB resident)")
    return _omni_model


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
            t = time.time()
            model.prepare_conditionals(ref_audio, sample_rate=model.sample_rate, exaggeration=EXAGGERATION)
            conds = model._conds
            log(f"encoded voice '{voice}' for Chatterbox in {time.time() - t:.1f}s (cached from now on)")
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

def omni_is_loaded() -> bool:
    return _omni_model is not None

def omni_is_cached() -> bool:
    """True once the OmniVoice weights are in the local HF cache. Checks the
    files that matter rather than the whole snapshot: mlx-audio only pulls what
    it loads, so snapshot_download(local_files_only=True) always reports the
    snapshot as "incomplete" (no README/.gitattributes) even after a full load."""
    try:
        from huggingface_hub import try_to_load_from_cache

        needed = ("config.json", "model.safetensors", "audio_tokenizer/model.safetensors")
        return all(isinstance(try_to_load_from_cache(OMNI_MODEL, f), str) for f in needed)
    except Exception:
        return False


def _get_omni_ref(model, voice: str, preset: dict):
    """OmniVoice reference-voice tokens, encoded once per voice and cached with
    the same file fingerprint as _get_conds (must hold _lock). None for voices
    without a clip -- OmniVoice then picks a voice of its own."""
    ref_audio = preset.get("ref_audio")
    if not ref_audio:
        return None
    fingerprint = (ref_audio, os.stat(ref_audio).st_mtime_ns)
    cached = _omni_ref_cache.get(voice)
    if cached is None or cached[0] != fingerprint:
        from mlx_audio.tts.models.omnivoice import create_voice_clone_prompt

        t = time.time()
        tokens = create_voice_clone_prompt(ref_audio, tokenizer=model.audio_tokenizer, max_duration_s=OMNI_REF_MAX_S)
        # Materialize now: the encoder returns a lazy array bound to *this*
        # thread's GPU stream. The pre-warm thread fills this cache, and a
        # request thread using an unevaluated array from it fails with
        # "There is no Stream(gpu, N) in current thread".
        import mlx.core as mx

        mx.eval(tokens)
        _omni_ref_cache[voice] = (fingerprint, tokens)
        log(f"encoded voice '{voice}' for OmniVoice in {time.time() - t:.1f}s (cached from now on)")
    return _omni_ref_cache[voice][1]


def _omni_wav(chunk: str, lang: str, voice: str, preset: dict) -> bytes:
    """One chunk through OmniVoice (must hold _lock). It yields a single result
    per call -- no streaming -- and does no chunking of its own."""
    model = get_engine("omnivoice")
    ref = _get_omni_ref(model, voice, preset)
    r = next(model.generate(text=chunk, language=lang, ref_tokens=ref, num_steps=OMNI_NUM_STEPS, verbose=False))
    return _wav_bytes(r.audio, r.sample_rate)


def choose_engine(engine: str, chunks: list[str]) -> str:
    """Resolve 'auto' to a single engine for the whole request: OmniVoice if any
    chunk is in a non-Latin script (it reads English too, so a mixed document
    never has to swap engines mid-request), else Chatterbox."""
    if engine in ENGINES:
        return engine
    return "omnivoice" if any(_script_language(c) for c in chunks) else "chatterbox"


def iter_speech(
    text: str,
    voice: str = "default",
    word_budget: int = CHUNK_WORD_BUDGET,
    stream: bool = True,
    client: str | None = None,
    engine: str = "auto",
):
    """Yield raw WAV bytes as speech is generated, so a caller can start
    playback long before the rest of the text is done.

    stream=True  -> pieces of ~1.6s arrive continuously *within* each chunk
                    (first sound in ~0.3s). Use for live playback.
    stream=False -> one piece per text chunk, ~1.5x less compute. Use when
                    nobody is listening until the whole thing is done.
    client       -> optional label (e.g. the requester's IP) for the log lines.
    engine       -> 'chatterbox' (English only, expressive tags, streams),
                    'omnivoice' (646 languages incl. English, cloning), or
                    'auto' (see choose_engine).
    """
    voices = load_voices()
    if voice not in voices:
        raise ValueError(f"Unknown voice '{voice}'. Available: {', '.join(sorted(voices))}")
    preset = voices[voice]

    chunks = split_into_chunks(text, word_budget)
    if not chunks:
        raise ValueError("No text to synthesize")

    import mlx.core as mx

    sr = SAMPLE_RATE
    use = choose_engine(engine, chunks)
    langs = [_script_language(c) for c in chunks]
    mix = ", ".join(f"{k or 'en'}:{langs.count(k)}" for k in dict.fromkeys(langs))
    who = f" for {client}" if client else ""
    swap = f", swapping from {engine_loaded()}" if engine_loaded() not in (None, use) else ""
    log(f"▶ '{voice}'{who}: {len(text.split())} words → {len(chunks)} chunks ({mix}) via {use}{swap}, {'streaming' if stream else 'batch'}")
    t_all = time.time()
    audio_all = 0.0
    done = 0

    def secs(b: bytes) -> float:  # 16-bit mono WAV bytes -> seconds of audio
        return max(0, len(b) - 44) / 2 / sr

    try:
        for i, chunk in enumerate(chunks, 1):
            lang = langs[i - 1]
            t_chunk = time.time()
            chunk_audio = 0.0

            if use == "omnivoice":
                # One piece per chunk (OmniVoice can't stream). The lock is not
                # held across the yield; get_engine() inside it swaps engines
                # if another request changed the resident one meanwhile.
                with _lock:
                    data = _omni_wav(chunk, lang or "en", voice, preset)
                chunk_audio = secs(data)
                yield data

            elif not stream:
                # Lock is held only around the model call, never across a `yield`
                # -- if the consumer (e.g. a disconnected HTTP client) never
                # resumes us, the lock must still be released.
                with _lock:
                    model = get_engine("chatterbox")
                    model._conds = _get_conds(model, voice, preset)  # restore the shared slot for this voice
                    results = list(model.generate(text=chunk, verbose=False))
                    wav = results[0].audio if len(results) == 1 else mx.concatenate([r.audio for r in results], axis=0)
                    data = _wav_bytes(wav, sr)
                chunk_audio = secs(data)
                yield data

            else:
                # Streaming: stream_generate() keeps model state live between
                # pieces, so the lock has to span the whole chunk. To avoid
                # holding it across our own `yield`s (the deadlock trap above), a
                # producer thread owns the lock and drops finished pieces into a
                # queue; it always runs the chunk to completion, so an abandoned
                # consumer wastes at most one chunk and can never wedge the lock.
                q: queue.Queue = queue.Queue()

                def produce(chunk=chunk):
                    try:
                        with _lock:
                            model = get_engine("chatterbox")
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
                    chunk_audio += secs(item)
                    yield item

            audio_all += chunk_audio
            done = i
            log(f"   {i}/{len(chunks)} [{lang or 'en'}] {len(chunk.split())} words → {chunk_audio:.1f}s audio in {time.time() - t_chunk:.1f}s")

        el = max(time.time() - t_all, 1e-6)
        log(f"✓ '{voice}'{who}: {audio_all:.1f}s of audio in {el:.1f}s ({audio_all / el:.1f}x real-time)")
    except GeneratorExit:
        log(f"⏹ '{voice}'{who}: client stopped after chunk {done}/{len(chunks)}")
        raise
    except Exception as e:
        log(f"✗ '{voice}'{who}: chunk {done + 1}/{len(chunks)} failed -- {type(e).__name__}: {e}")
        raise
    finally:
        mx.clear_cache()  # hand MLX's scratch buffers back between requests


def synthesize(text: str, voice: str = "default", word_budget: int = CHUNK_WORD_BUDGET, engine: str = "auto") -> Path:
    """Generate speech and return a path to one concatenated wav file
    (batch mode: waits for everything). Caller must delete path.parent."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="tts_"))
    try:
        chunk_paths = []
        for i, data in enumerate(iter_speech(text, voice, word_budget, stream=False, engine=engine)):
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
    """Load the default engine and run a throwaway generation so the first real
    request isn't slowed by MLX's one-time kernel compilation. Deliberately
    lets any failure propagate: if the model can't load, better to die at
    startup than to print "Ready." and then fail every request."""
    if DEFAULT_ENGINE == "omnivoice":
        for _ in iter_speech("Hello there.", "default", engine="omnivoice"):
            pass
        return
    for _ in iter_speech("Hello there.", "default", stream=False, engine="chatterbox"):
        pass
    for _ in iter_speech("Hello there.", "default", stream=True, engine="chatterbox"):  # streaming kernels too
        pass


def prewarm_voices() -> None:
    """Compute conditioning for every voice for the *resident* engine so the
    first pick of each one is instant. Meant to run in a background thread
    after startup: each voice takes _lock briefly, so a real request that
    arrives mid-way just waits for the current voice (~1s), not for all of
    them. The other engine is loaded lazily on first use and encodes voices
    as they're picked (~1s each, once)."""
    voices = load_voices()
    ok = 0
    for name, preset in voices.items():
        try:
            with _lock:
                use = engine_loaded() or DEFAULT_ENGINE
                model = get_engine(use)
                if use == "chatterbox":
                    _get_conds(model, name, preset)
                else:
                    _get_omni_ref(model, name, preset)
            ok += 1
        except Exception as e:  # one bad clip shouldn't stop the rest
            log(f"prewarm: skipping voice '{name}': {e}")
        # threading.Lock isn't fair: re-acquiring immediately in a tight loop
        # can starve a real request for the whole pre-warm (~20s). A short
        # pause hands the lock to anyone waiting.
        time.sleep(0.1)
    log(f"prewarm: all {ok}/{len(voices)} voices ready for {engine_loaded()}. {active_memory_gb():.1f} GB resident.")
