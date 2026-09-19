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
import re
import shutil
import tempfile
import threading
import wave
from pathlib import Path

MODEL = "mlx-community/chatterbox-turbo-fp16"
ROOT = Path(__file__).parent
VOICES_FILE = ROOT / "voices.json"
EXAGGERATION = 0.5  # ignored by Turbo's generation, only affects conds caching key

# Conservative per-chunk word budget: a single generation call is capped at
# ~800-1200 speech tokens with no built-in sentence splitting, so long text
# must be chunked ourselves to avoid audio being silently cut off mid-sentence.
CHUNK_WORD_BUDGET = 50

_model = None
_conds_cache: dict = {}
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


def split_into_chunks(text: str, word_budget: int = CHUNK_WORD_BUDGET) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
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
            if len(words) > word_budget:
                if current:
                    chunks.append(" ".join(current))
                    current, current_words = [], 0
                for i in range(0, len(words), word_budget):
                    chunks.append(" ".join(words[i : i + word_budget]))
                continue
            if current and current_words + len(words) > word_budget:
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
    if voice not in _conds_cache:
        ref_audio = preset.get("ref_audio")
        if ref_audio:
            model.prepare_conditionals(ref_audio, sample_rate=model.sample_rate, exaggeration=EXAGGERATION)
            _conds_cache[voice] = model._conds
        else:
            _conds_cache[voice] = _builtin_conds
    return _conds_cache[voice]


def iter_speech(text: str, voice: str = "default", word_budget: int = CHUNK_WORD_BUDGET):
    """Yield raw WAV bytes for each chunk of `text`, as soon as it's generated.

    Lets a caller start playback before the rest of the text is done, and
    reuses one cached voice-conditioning computation across all chunks.
    """
    voices = load_voices()
    if voice not in voices:
        raise ValueError(f"Unknown voice '{voice}'. Available: {', '.join(sorted(voices))}")
    preset = voices[voice]

    chunks = split_into_chunks(text, word_budget)
    if not chunks:
        raise ValueError("No text to synthesize")

    import io

    import mlx.core as mx
    from mlx_audio.audio_io import write as audio_write

    model = get_model()
    for chunk in chunks:
        # Lock is held only around the actual model call, never across a
        # `yield` -- if a consumer (e.g. a disconnected HTTP client) never
        # resumes this generator, the lock must still get released instead
        # of wedging every other request forever.
        with _lock:
            conds = _get_conds(model, voice, preset)
            model._conds = conds  # restore: another voice's request may have overwritten this shared slot
            results = list(model.generate(text=chunk, verbose=False))
            wav = results[0].audio if len(results) == 1 else mx.concatenate([r.audio for r in results], axis=0)
            buf = io.BytesIO()
            audio_write(buf, wav, results[0].sample_rate, format="wav")
            data = buf.getvalue()
        yield data


def synthesize(text: str, voice: str = "default", word_budget: int = CHUNK_WORD_BUDGET) -> Path:
    """Generate speech and return a path to one concatenated wav file
    (batch mode: waits for everything). Caller must delete path.parent."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="tts_"))
    try:
        chunk_paths = []
        for i, data in enumerate(iter_speech(text, voice, word_budget)):
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
    MLX's one-time kernel compilation."""
    try:
        for _ in iter_speech("Hello.", "default"):
            pass
    except Exception:
        pass
