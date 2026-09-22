# bokbok -- A natural free flow speech generator over flux 🤖


## uses chatterbox Turbo (MLX)

Fast, expresive local TTS using [Chatterbox Turbo](https://www.resemble.ai/chatterbox-turbo/) via the
community [mlx-audio](https://github.com/Blaizzy/mlx-audio) port (`mlx-community/chatterbox-turbo-fp16`),
running natively on AppleSilicon (MLX/Metal) instead of the official PyTorch package — Because I have a blazing fast mbp.

## Setup (just one-time)

```bash
pip3 install --user uv               # if not already installed
echo 'export PATH="$HOME/Library/Python/3.14/bin:$PATH"' >> ~/.zshrc # otherwise zsh wont be able to find your uv installation
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

`read_aloud.py` splits the file into sentence-level chunks itself (the first one deliberately short,
so `--play` starts talking within a second or two), generates each with the chosen voice preset, and
stitches the results into one `.wav`. (The model has its own internal ~800-token splitting too, but
chunking ourselves first is what lets one chunk = one playable/streamable unit — see Performance
below.)

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

To add a new preset by hand: drop a clean, single-speaker `.wav`/`.mp3` clip (6+ seconds) into
`voices/`, then add an entry to `voices.json` pointing `ref_audio` at it. (The `ref_text` field is
vestigial — Chatterbox Turbo never reads a transcript — so leave it `null`.) Or just use the "Add a
voice" panel in the web UI, which does all of this for you.

**`.m4a` clips**: mlx-audio needs `ffmpeg` to decode `.m4a`/`.aac`, which isn't installed here (kept
minimal, no Homebrew). Convert once with macOS's built-in `afconvert` instead:
```bash
afconvert -f WAVE -d LEI16 clip.m4a voices/voiceover_sample_actors/clip.wav
```

## Bengali, Hindi and other scripts

Chatterbox Turbo is **English-only** — its tokenizer is stock GPT-2 byte-level BPE, so text in
another script doesn't error, it silently shatters into junk tokens and comes out as noise. (Even
Chatterbox Multilingual's 23 languages don't include Bengali.) So `tts_core.py` routes chunks written
in these scripts to a second engine, **OmniVoice** (`mlx-community/OmniVoice-bfloat16`, also inside
mlx-audio: 646 languages, Apache-2.0, 24kHz), and keeps everything else on Chatterbox:

| Script | Sent to OmniVoice as |
|---|---|
| Bengali (বাংলা) | `bn` |
| Devanagari / Hindi (हिन्दी) | `hi` |

- Detection is automatic, **per chunk**: a mixed English/Bengali document just works, and a Bengali
  sentence containing a few English words (like "Facebook") still counts as Bengali. Sentences are
  split at "।" as well as ".!?".
- The **selected voice is cloned cross-lingually** from its reference clip (encoded once per voice
  and cached): a native Bengali recording sounds native; an English actor's clip reads Bengali with an
  English accent. The `default` voice lets OmniVoice pick its own.
- The first Bengali/Hindi request **downloads OmniVoice once (~3GB)** and loads it (~30s); after that
  it's pre-loaded in the background at startup, so there's no delay. Nobody who only speaks English
  ever pays for it.
- OmniVoice can't stream within a chunk (one piece per chunk), so its chunks are kept to ~25 words
  (~10s of audio; `OMNI_WORD_BUDGET`) and the first one shorter still — first sound in ~3s, then it
  generates ~8x faster than real-time, so playback never waits. `OMNI_NUM_STEPS` (32) trades quality
  for speed (16 is ~2x faster); `SCRIPT_LANGS` is where to add another script → language-tag mapping.
- If a Bengali/Hindi request is sent within the first minute after a restart, the page says it's
  loading the engine (or downloading it, the very first time) rather than looking stuck. Any failure
  during generation is reported in the status line (the stream carries an in-band error frame), so a
  problem never just shows up as silence.

## Sharing it over Wi-Fi (REST API)

```bash
.venv/bin/python app.py
```

This starts a tiny Flask server (`app.py` serving `index.html`, on top of the same `tts_core.py`
chunking/voice logic `read_aloud.py` uses) and prints three URLs — one of them will work for anyone
else on the same Wi-Fi network, no install needed on their end:

```
Local:   http://127.0.0.1:5050/
Wi-Fi:   http://<your-lan-ip>:5050/
Bonjour: http://<your-mac-name>.local:5050/   # works on most Macs/iPhones, no IP needed
```

Anyone who opens that URL in a browser gets:
- A voice picker (grouped by accent) and a text box, with quick-insert buttons for the emotion tags.
- **Load text file** — or just drag a `.txt`/`.md` anywhere onto the card — fills the text box and
  shows a word count and estimated speaking time. Dropping an audio file instead sends it to "Add a
  voice" below.
- **Speak** — generates and plays back progressively in their own browser (see Performance below).
- A **player bar** with play/pause, elapsed time, a seek slider and total time — the usual native
  media controls, rebuilt on top of the streaming player (a real `<audio>` element can't consume a
  stream). It appears as soon as the first audio lands; the total keeps growing (shown with a `+`)
  until generation finishes, and you can seek back through the last 15 minutes even while it's still
  generating. Pressing play at the end replays from the start.
- A **speed** dropdown (0.75x-2x) — applies live to whatever's playing.
- **🗑️ Clear** — stops and discards the loaded audio (cancelling an in-progress generation if there is
  one). Your text, voice selection and speed are left exactly as they were.
- **Add a voice** — record from the mic, or upload an existing `.wav`/`.mp3`/`.m4a`/etc. clip, give it a
  name, and it's usable for Speak immediately (no restart). Shows up under "Your recordings" in the
  voice picker, with a 🗑 next to the picker to delete it again. **Mic recording only works when this
  page is opened as `http://127.0.0.1:5050` or `http://localhost:5050` on this Mac** — browsers block
  microphone access over plain HTTP from any other device (the normal way this app is shared on Wi-Fi).
  The file-upload fallback works from any device regardless. Only voices added this way can be deleted —
  the curated library above can't be removed through the UI.

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

curl -X POST http://<host>:5050/voices \
  -F "name=My Voice" -F "audio=@clip.wav"                         # add a voice (clip must be a WAV, >5s)

curl -X DELETE http://<host>:5050/voices/custom_my_voice          # only custom_* voices can be deleted
```

Notes:
- Port defaults to `5050` (macOS's AirPlay Receiver squats on `5000`); override with `PORT=8080 .venv/bin/python app.py`.
- The model is warmed up at startup (if that fails, the server exits loudly rather than printing
  "Ready." and failing every request). Every voice's conditioning is then pre-computed in the
  background, so the first pick of any voice is instant.
- The server is threaded: page loads, the voice list and uploads stay responsive while someone's long
  generation runs. Model access itself is serialized (the MLX model isn't safe for concurrent calls),
  so two people generating at once interleave chunk-by-chunk — both still hear progressive audio.
- Text is capped at 300,000 characters per request (roughly 6-7 hours of speech) — long texts are a
  real use case, and streaming means playback starts in under a second regardless; the cap only
  stops a stray paste from tying up the shared model indefinitely. Change `MAX_TEXT_CHARS` in both
  `app.py` and `index.html` to adjust.
- Edits to `index.html` show up on a browser refresh; edits to the `.py` files need a server restart.
- This is Flask's dev server, fine for a local/trusted Wi-Fi network, not meant for the open internet.
- **The server log tells you what's going on**: who opened the page (IP + device), each request as
  `▶ 'voice' for <ip>: N words → M chunks (en:.., bn:..), streaming`, one line per chunk with its
  engine and timing, then `✓ … 3.4x real-time`, or `⏹ client stopped after chunk k/M`, or
  `✗ … failed -- <error>`. First-time voice encodes and engine loads are logged too. The dev server's
  own per-request access lines and the models' progress bars are silenced.
- **Phones**: iPhone/iPad only allow audio to start from a tap, so playback begins when you press
  Speak (or ▶ on the player) — the page resumes the audio engine on the first touch. If it's silent,
  check the **ring/silent switch** (Safari mutes web audio when it's on) and the volume. The page
  holds a screen wake-lock while playing so a long text doesn't stop when the screen would lock.

## Performance

Three things make generation and playback noticeably faster/snappier:

- **Cached voice conditioning.** Cloning a voice from a reference clip requires a forward pass over
  that clip (voice encoder + tokenizer). The obvious way to call the model recomputes this on *every*
  chunk; `tts_core.py` computes it once per voice and reuses it, which measured ~25-30% faster overall
  for multi-chunk cloned-voice text. The cache is keyed on the clip's path *and* mtime, so
  repointing or replacing a voice's file is picked up automatically — no stale-voice bugs, no
  invalidation calls to remember.
- **Sub-chunk streaming.** `iter_speech(stream=True)` (used by `POST /speak/stream`) drives the
  model's `stream_generate()`, which hands back ~1.6s of audio every 40 speech tokens *within* a
  chunk. Measured time-to-first-audio: **~0.3s**, versus 2-3s when waiting for a whole chunk. It
  re-vocodes the cumulative token sequence each time, so it costs ~1.5x the compute of a plain
  `generate()` — but it still runs 3-4x faster than real-time, so a listener never waits, and the
  seams between pieces are sample-continuous (verified: no clicks). The web UI decodes each piece
  with the Web Audio API and schedules it back-to-back as it arrives (frames on the wire are
  `<4-byte length><wav bytes>`). Batch callers — `/speak`, `read_aloud.py` — use `stream=False`
  (one piece per sentence-level chunk, the cheaper `generate()`) since nobody's listening until
  they're done; `read_aloud.py --play` also uses it because it plays pieces through separate
  `afplay` processes, where 1.6s pieces would leave audible gaps.
- **A short first chunk.** Text is split into ~50-word chunks, but the first is capped at ~15 words
  so even the batch/CLI path starts producing sound quickly.

(`chatterbox_turbo.py` does its own internal ~800-token/chunk text splitting too; we still chunk
ourselves first because that's what gives each chunk a clean boundary to stream and to stitch.)

The web player keeps only the most recent 15 minutes of a long text seekable — decoded audio is
float32 in RAM (~5.8MB/minute) and keeping a whole chapter around could crash a phone tab. Older
audio is dropped as it goes and the slider's left edge moves up, like a live-stream DVR window
(`KEEP_SECONDS` in `index.html`).

**Correctness gotcha this all works around:** the model keeps "the current voice" in a single shared
`model._conds` slot — `prepare_conditionals()` sets it as a side effect (it has no return value you can
cache), and `generate()` always reads from that same slot (it has no `conds=` parameter to pass one
explicitly). So `tts_core.py` snapshots that slot per voice and explicitly restores the right snapshot
before every generation call — skipping that step is what caused an earlier bug where switching voices
between requests could silently keep speaking in whichever voice was used most recently.

If you need it faster still, `mlx-community` also hosts 8-bit/4-bit quantized Turbo checkpoints
(smaller/faster, some quality trade-off) — swap `MODEL` in `tts_core.py` to try one.
