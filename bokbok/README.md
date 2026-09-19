[README.md](https://github.com/user-attachments/files/32416909/README.md)
# Chatterbox Turbo (MLX)

Fast, expressive local TTS using [Chatterbox Turbo](https://www.resemble.ai/chatterbox-turbo/) via the
community [mlx-audio](https://github.com/Blaizzy/mlx-audio) port (`mlx-community/chatterbox-turbo-fp16`),
running natively on Apple Silicon (MLX/Metal) instead of the official PyTorch package — this avoids a
known PyTorch-MPS crash bug and is faster on this hardware.

## Setup (one-time)

```bash
pip3 install --user uv               # if not already installed
uv venv --python 3.11 .venv          # isolated Python 3.11, doesn't touch system Python
uv pip install --python .venv -U mlx-audio flask
```

## Generate speech

```bash
.venv/bin/mlx_audio.tts.generate \
  --model mlx-community/chatterbox-turbo-fp16 \
  --text "Hi there! [chuckle] Great to finally get this working." \
  --file_prefix output --play
```

- Omit `--ref_audio` to use the default built-in voice; pass `--ref_audio path.wav` to clone a voice.
- The model (~3GB) downloads automatically from Hugging Face on first run.

## Emotion tags

Insert these inline in `--text` for expressiveness:
`[clear throat]` `[sigh]` `[shush]` `[cough]` `[groan]` `[sniff]` `[gasp]` `[chuckle]` `[laugh]`

Note: `--exaggeration` and `--cfg_scale` are ignored by Turbo (only apply to the base Chatterbox model).

## Reading a text file aloud with a voice preset

```bash
.venv/bin/python read_aloud.py mystory.txt --voice default --play
.venv/bin/python read_aloud.py mystory.txt --voice uk_male_norman -o story.wav
```

`read_aloud.py` splits the file into sentence-level chunks itself, generates each with the chosen
voice preset, and stitches the results into one `.wav`. (The model has its own internal ~800-token
splitting too, but chunking ourselves first is what lets one chunk = one playable/streamable unit —
see Performance below.)

### Voice presets

Chatterbox Turbo has **no named preset voices** (no ElevenLabs-style voice library) — the only way to
get a distinct voice is by cloning one from a short reference clip. Presets are defined in
[`voices.json`](voices.json), each pointing at a clip under `voices/`:

| Preset | Description |
|---|---|
| `default` | Model's built-in voice (no cloning) |
| `uk_female_abigail` | British female (Abigail, vagabond) |
| `uk_female_liss` | British female (Liss, Lulworth) |
| `uk_female_sarah` | British female (Sarah) |
| `uk_female_esther_irish_accent` | English speaker with an Irish accent (Esther) |
| `uk_female_cathy` | British female (Cathy K.) |
| `uk_female_julie_ann` | British female (Julie-Ann D.) |
| `uk_girl_julie` | British young female/girl (Julie) |
| `uk_male_david` | British male (David S.) |
| `uk_male_jethro` | British male (Jethro) |
| `uk_male_marc` | British male (Marc C.) |
| `uk_male_mike` | British male (Mike C.) |
| `uk_male_norman` | British male (Norman G.) |
| `uk_male_peter` | British male (Peter B.) |
| `uk_male_simon` | British male (Simon P.) |
| `us_female_kim` | American female (Kim W.) |
| `us_male_dcg` | American male (DCG) |
| `hindi_female_ad` | Hindi female (AD) |
| `hindi_female_anjum` | Hindi female (Anjum S.) |
| `hindi_female_khan` | Hindi female (Khan) |
| `hindi_female_vo01` | Hindi female voiceover (01) |
| `hindi_male_darshan` | Hindi male (Darshan) |

Source clips live in `voices/voiceover_sample_actors/` (voiceover demo reels, mostly ~20-25s but some
up to ~2min; only the first ~10s actually gets used for conditioning regardless of clip length).
Listen through them and, once you've picked a favorite for narration, consider adding a friendly
alias (e.g. `british_storyteller`) pointing at the same file in `voices.json`.

To add a new preset of your own: drop a clean, single-speaker `.wav`/`.mp3` clip (6+ seconds) into
`voices/`, then add an entry to `voices.json` pointing `ref_audio` at it (an exact transcript in
`ref_text` can improve cloning fidelity, but it's optional).

**`.m4a` clips**: mlx-audio needs `ffmpeg` to decode `.m4a`/`.aac`, which isn't installed here (kept
minimal, no Homebrew). Convert once with macOS's built-in `afconvert` instead:
```bash
afconvert -f WAVE -d LEI16 clip.m4a voices/voiceover_sample_actors/clip.wav
```

## Sharing it over Wi-Fi (REST API)

```bash
.venv/bin/python app.py
```

This starts a tiny Flask server (`app.py` + `tts_core.py`, sharing the same chunking/voice logic as
`read_aloud.py`) and prints three URLs — one of them will work for anyone else on the same Wi-Fi
network, no install needed on their end:

```
Local:   http://127.0.0.1:5050/
Wi-Fi:   http://<your-lan-ip>:5050/
Bonjour: http://<your-mac-name>.local:5050/   # works on most Macs/iPhones, no IP needed
```

Anyone who opens that URL in a browser gets:
- A voice picker (grouped by accent) and a text box, with quick-insert buttons for the emotion tags.
- **Speak** — generates and plays back progressively in their own browser (see Performance below).
- **⏸ / ▶** — pause/resume playback (appears once something starts playing).
- A **speed** dropdown (0.75x-2x) — applies live, even to audio already playing/queued.
- **↻ Replay** — instantly replays the last result with no new request. It auto-hides the moment you
  change the voice dropdown, so it can't accidentally play the old voice back under a new selection.
- **🗑️ Clear** — resets everything *except the text box*: cancels an in-progress generation if one's
  running, stops any playing/queued audio, discards the downloaded/decoded audio from the browser,
  resets speed back to 1x, and resets the voice picker to its first option.

It's also a plain REST API:

```bash
curl http://<host>:5050/voices                                   # list presets
curl -X POST http://<host>:5050/speak \
  -H "Content-Type: application/json" \
  -d '{"text": "Hi there! [chuckle]", "voice": "uk_male_norman"}' \
  -o out.wav                                                      # one complete .wav file

curl -X POST http://<host>:5050/speak/stream \
  -H "Content-Type: application/json" \
  -d '{"text": "Hi there! [chuckle]", "voice": "uk_male_norman"}' \
  -o out.stream                                                   # chunks framed as <4-byte length><wav bytes>
```

Notes:
- Port defaults to `5050` (macOS's AirPlay Receiver squats on `5000`); override with `PORT=8080 .venv/bin/python app.py`.
- The model is warmed up once at server startup (not on the first request) and reused; generation is
  serialized (one chunk at a time across all requests) since the MLX model isn't safe for concurrent
  calls — fine for casual multi-person use on a LAN.
- This is Flask's dev server, fine for a local/trusted Wi-Fi network, not meant for the open internet.

## Performance

Two things make generation and playback noticeably faster/snappier:

- **Cached voice conditioning.** Cloning a voice from a reference clip requires a forward pass over
  that clip (voice encoder + tokenizer). The obvious way to call the model recomputes this on *every*
  chunk; `tts_core.py` computes it once per voice and reuses it, which measured ~25-30% faster overall
  for multi-chunk cloned-voice text (bigger savings the more chunks/voices reused across requests).
- **Streaming instead of batching.** `iter_speech()` yields each chunk's audio as soon as it's
  generated. `read_aloud.py --play` starts playing chunk 1 while chunk 2 is still generating; the web
  UI does the same over HTTP via `POST /speak/stream` (chunks framed as `<4-byte length><wav bytes>`)
  and the Web Audio API, scheduling each decoded chunk to play back-to-back as it arrives — so on a
  multi-sentence request you hear the first sentence long before the last one has finished generating.
  (`/speak` still exists and returns one complete file for curl/simple-download use.)

Not yet used: the model actually has a `stream_generate()` method that yields audio every ~40 tokens
*within* a chunk (finer-grained than our sentence-level chunking), for even lower time-to-first-audio
on a single long sentence. We don't call it currently — everything above streams only *between* our
own chunks. (`chatterbox_turbo.py` also does its own internal ~800-token/chunk splitting when you just
call `generate()` directly, same as we do — we still chunk ourselves first because that's what gives
each chunk a clean boundary to stream as its own HTTP frame / playback unit.)

**Correctness gotcha this all works around:** the model keeps "the current voice" in a single shared
`model._conds` slot — `prepare_conditionals()` sets it as a side effect (it has no return value you can
cache), and `generate()` always reads from that same slot (it has no `conds=` parameter to pass one
explicitly). So `tts_core.py` snapshots that slot per voice and explicitly restores the right snapshot
before every generation call — skipping that step is what caused an earlier bug where switching voices
between requests could silently keep speaking in whichever voice was used most recently.

If you need it faster still, `mlx-community` also hosts 8-bit/4-bit quantized Turbo checkpoints
(smaller/faster, some quality trade-off) — swap `MODEL` in `tts_core.py` to try one.
