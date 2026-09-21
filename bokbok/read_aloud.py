#!/usr/bin/env python3
"""Read a text file aloud with Chatterbox Turbo, using a named voice preset.

Usage:
    .venv/bin/python read_aloud.py mystory.txt --voice default --play
    .venv/bin/python read_aloud.py mystory.txt --voice uk_male_norman -o story.wav

With --play, each chunk starts playing as soon as it's generated instead of
waiting for the whole file to finish. Voice presets are defined in
voices.json (see that file / README for how to add one).
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tts_core import CHUNK_WORD_BUDGET, MODEL, concatenate_wavs, iter_speech, load_voices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("text_file", type=Path, help="Path to a .txt file to read aloud")
    parser.add_argument("--voice", default="default", help="Voice preset name from voices.json")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output .wav path (default: <text_file>.wav)")
    parser.add_argument("--play", action="store_true", help="Play each chunk as soon as it's generated")
    parser.add_argument("--chunk-words", type=int, default=CHUNK_WORD_BUDGET, help="Max words per generation chunk")
    args = parser.parse_args()

    if args.voice not in load_voices():
        sys.exit(f"Unknown voice preset '{args.voice}'. Available: {', '.join(sorted(load_voices()))}")

    output_path = args.output or args.text_file.with_suffix(".wav")
    text = args.text_file.read_text()

    print(f"Loading {MODEL} ...")
    tmp_dir = Path(tempfile.mkdtemp(prefix="read_aloud_"))
    chunk_paths: list[Path] = []
    now_playing: subprocess.Popen | None = None
    try:
        # stream=False on purpose: --play hands each piece to a separate afplay
        # process, so ~1.6s streaming pieces would leave audible gaps between
        # them. Sentence-sized chunks keep the gaps at natural pauses.
        for i, data in enumerate(iter_speech(text, args.voice, args.chunk_words, stream=False)):
            path = tmp_dir / f"part_{i:04d}.wav"
            path.write_bytes(data)
            chunk_paths.append(path)
            print(f"[{i + 1}] generated ({len(data) / 1024:.0f} KB)")
            if args.play:
                if now_playing is not None:
                    now_playing.wait()  # don't overlap playback, but generation of the next chunk already ran while this one played
                now_playing = subprocess.Popen(["afplay", str(path)])
        if now_playing is not None:
            now_playing.wait()
    except ValueError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(str(e))

    try:
        concatenate_wavs(chunk_paths, output_path)
        print(f"\nSaved: {output_path}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
