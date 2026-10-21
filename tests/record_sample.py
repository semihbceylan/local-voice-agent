#!/usr/bin/env python3
"""
Record a raw microphone sample for endpointer tuning.

The sample is stored without any VAD processing, so the endpointer can be
evaluated against the real microphone signal offline.

Usage:
    .venv/bin/python tests/record_sample.py --seconds 12
    .venv/bin/python tests/record_sample.py --seconds 12 --out tests/fixtures/mic_sample.wav
"""

import argparse
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 16000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--device", default=":0")
    parser.add_argument("--out", default=str(ROOT / "tests" / "fixtures" / "mic_sample.wav"))
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 68)
    print("  Raw microphone sample (no VAD, no trimming)")
    print("=" * 68)
    print(f"  device : {args.device}")
    print(f"  length : {args.seconds:.0f} s")
    print(f"  output : {out}")
    print()
    print("  Speak the following when the recording starts, then stay quiet:")
    print('      "Where is my order 1001?"')
    print()
    print("  Starting in 3 seconds...")
    for remaining in (3, 2, 1):
        print(f"    {remaining}", end="\r", flush=True)
        time.sleep(1)
    print("    GO  " + " " * 20, flush=True)

    raw = out.with_suffix(".raw.wav")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-i", args.device,
        "-t", str(args.seconds),
        "-ar", str(SAMPLE_RATE), "-ac", "1",
        str(raw),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"\n  RECORDING FAILED: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    with wave.open(str(raw), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate()
    print(f"\n  Recorded {frames / rate:.2f} s at {rate} Hz")

    # Keep a 16 kHz mono copy next to the raw file for inspection.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
         "-ar", str(SAMPLE_RATE), "-ac", "1", str(out.with_name(out.stem + "_16k.wav"))],
        check=True,
    )
    print(f"  Sample written to {out}")
    print("\n  Analyse it with:")
    print(f"    .venv/bin/python tests/analyse_sample.py {out}")


if __name__ == "__main__":
    main()
