#!/usr/bin/env python3
"""Read a text file aloud with Kokoro (local ONNX model). Used by render_reel.py for the narration.

    python speak_kokoro.py --text-file in.txt --out out.wav [--model-dir C:\\kokoro] [--voice af_heart] [--speed 1.0]

Needs the kokoro-onnx, soundfile and onnxruntime packages, and the two model files in --model-dir:
kokoro-v1.0.onnx and voices-v1.0.bin. Runs offline. No keys, no network.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_MODEL_DIR = Path("C:/kokoro")
DEFAULT_VOICE = "af_heart"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--speed", type=float, default=1.0)
    args = ap.parse_args()

    model_dir = Path(args.model_dir)
    model = model_dir / "kokoro-v1.0.onnx"
    voices = model_dir / "voices-v1.0.bin"
    for f in (model, voices):
        if not f.exists():
            print(f"STOP: Kokoro file missing: {f}")
            return 1

    import soundfile as sf
    from kokoro_onnx import Kokoro

    text = Path(args.text_file).read_text(encoding="utf-8").strip()
    if not text:
        print("STOP: nothing to speak")
        return 1
    kokoro = Kokoro(str(model), str(voices))
    samples, rate = kokoro.create(text, voice=args.voice, speed=args.speed, lang="en-us")
    sf.write(args.out, samples, rate)
    print(f"wrote {args.out} ({len(samples) / rate:.1f} s, voice {args.voice})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
