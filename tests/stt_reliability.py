#!/usr/bin/env python3
"""
Speech recognition reliability test.

Real speech is generated locally with Kokoro, transcribed with whisper.cpp and
then parsed with the production order parser. This checks the complete
"spoken order number -> parsed order id" chain instead of testing the parser
with hand written text only.

Usage:
    .venv/bin/python tests/stt_reliability.py
    .venv/bin/python tests/stt_reliability.py --prompt "Customer support: order number 1001"
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

PARSER_FILE = ROOT / "workflow" / "extract_order_id.js"

CASES = [
    ("Where is my order one thousand and one?", "1001"),
    ("Where is my order one thousand and two?", "1002"),
    ("Where is my order one thousand and three?", "1003"),
    ("My order number is one thousand and one.", "1001"),
    ("My order number is one thousand and two.", "1002"),
    ("Can you check order one thousand and three please?", "1003"),
    ("I would like to know the status of order one thousand and one.", "1001"),
]


def parse_with_production_parser(text: str) -> dict:
    """Run the same parser the workflow uses (workflow/extract_order_id.js)."""
    script = (
        "const {extractOrderIds}=require(process.argv[1]);"
        "let input='';process.stdin.on('data',d=>input+=d);"
        "process.stdin.on('end',()=>{console.log(JSON.stringify(extractOrderIds(input.trim())))});"
    )
    result = subprocess.run(
        ["node", "-e", script, str(PARSER_FILE)],
        input=text, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout.strip())


def synthesize(text: str, path: Path, voice: str = "af_heart") -> None:
    """Synthesize with Kokoro and convert to the 16 kHz mono whisper expects."""
    from mlx_audio.tts import utils
    import soundfile as sf

    model = utils.load("mlx-community/Kokoro-82M-bf16")
    segments = list(model.generate(text, voice=voice, lang_code="a"))
    audio = np.concatenate([np.array(segment.audio) for segment in segments])
    raw = path.with_suffix(".raw.wav")
    sf.write(str(raw), audio, int(segments[0].sample_rate))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
         "-ar", "16000", "-ac", "1", str(path)],
        check=True,
    )
    raw.unlink()


def transcribe(path: Path, prompt=None) -> tuple[str, float]:
    cmd = [
        "/opt/homebrew/bin/whisper-cli",
        "-m", str(ROOT / "ggml-base.en.bin"),
        "-f", str(path),
        "-otxt", "-nt", "-np", "-l", "en",
    ]
    if prompt:
        cmd += ["--prompt", prompt]
    start = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    elapsed = time.perf_counter() - start
    txt = path.with_name(path.name + ".txt")
    text = txt.read_text().strip() if txt.exists() else ""
    if txt.exists():
        txt.unlink()
    if not text and result.returncode != 0:
        print(f"        whisper failed: {result.stderr.strip()[:200]}")
    return " ".join(line.strip() for line in text.splitlines() if line.strip()), elapsed


def main():
    model = ROOT / "ggml-base.en.bin"
    if not model.exists():
        print(
            "skipped: the Whisper model is missing. Run ./scripts/setup.sh "
            "or download ggml-base.en.bin."
        )
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--keep-audio", default=None, help="directory for the generated wav files")
    args = parser.parse_args()

    print("=" * 78)
    print("  STT reliability: spoken order number -> whisper.cpp -> parser")
    print("=" * 78)
    if args.prompt:
        print(f"  whisper prompt: {args.prompt!r}")
    print()

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for index, (spoken, expected) in enumerate(CASES):
            wav = Path(tmp) / f"case_{index}.wav"
            synthesize(spoken, wav)
            transcript, stt_s = transcribe(wav, args.prompt)
            decision = parse_with_production_parser(transcript)
            got = decision.get("orderId")
            ok = got == expected
            failures += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'}  said: {spoken}")
            print(f"        heard: {transcript!r} ({stt_s:.2f}s)")
            print(
                f"        parsed: route={decision.get('route')} order={got or '-'} "
                f"confidence={decision.get('confidence')}"
            )
            if args.keep_audio:
                target = Path(args.keep_audio)
                target.mkdir(parents=True, exist_ok=True)
                (target / f"case_{index}.wav").write_bytes(wav.read_bytes())

    print(f"\n  {len(CASES) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
