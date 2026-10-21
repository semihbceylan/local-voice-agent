#!/usr/bin/env python3
"""
Physical microphone test for the voice agent.

This script has to be run by a human: it asks you to speak a set of
phrases, records them with the VAD, transcribes them, sends them to the
n8n voice webhook and prints the timing breakdown of every stage.

Usage:
    .venv/bin/python tests/live_mic_test.py
    .venv/bin/python tests/live_mic_test.py --silence-ms 800 --device :0

Before running, make sure the microphone is connected and that the terminal
has microphone permission (System Settings > Privacy & Security > Microphone).

The script never plays the answer, so it can be run in a quiet room without
the TTS interfering with the recording.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import voice_agent as va  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

PROMPTS = [
    ("Where is my order 1001?", "1001"),
    ("Check order 1,001", "1001"),
    ("Check order one thousand and one", "1001"),
    ("My order number is 1,000 and 1.", "1001"),
    ("Where is my order 1002?", "1002"),
    ("Check order 1003", "1003"),
    ("What about order 9999?", "9999"),
    ("How do I return a product?", None),
]


def list_devices(cfg) -> str:
    result = subprocess.run(
        [cfg.ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True, timeout=20,
    )
    return (result.stderr or "") + (result.stdout or "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default=os.environ.get("AUDIO_DEVICE", ":0"))
    parser.add_argument("--silence-ms", type=int, default=800)
    parser.add_argument("--webhook", default=os.environ.get(
        "N8N_WEBHOOK_URL", "http://localhost:5678/webhook/voice-agent"))
    parser.add_argument("--out", default=None, help="write the report as JSON")
    parser.add_argument(
        "--play", action="store_true",
        help="also speak the answer (the speaker can bleed into the next recording)",
    )
    args = parser.parse_args()

    cfg = va.Config()
    cfg.audio_device = args.device
    cfg.webhook_url = args.webhook
    cfg.vad_silence_ms = args.silence_ms

    print("=" * 72)
    print("  Live microphone test - DemoShop voice agent")
    print("=" * 72)
    print(f"  device : {cfg.audio_device}")
    print(f"  webhook: {cfg.webhook_url}")
    print(f"  silence threshold: {cfg.vad_silence_ms} ms")
    print()
    print("  AVFoundation devices:")
    for line in list_devices(cfg).splitlines():
        if "AVFoundation" in line or "input" in line.lower() or "[" in line:
            print("   ", line.strip())
    print()

    report = []
    for text, expected in PROMPTS:
        print("-" * 72)
        print(f"  PLEASE SAY: {text!r}")
        input("  Press Enter when you are ready, then speak and stay quiet -> ")

        turn_dir = Path(ROOT / "logs" / "mic_turns")
        turn_dir.mkdir(parents=True, exist_ok=True)
        turn_path = turn_dir / f"turn_{len(report) + 1:02d}.wav"
        try:
            _, capture_s, metrics = va.record_vad(cfg, str(turn_path))
        except Exception as exc:  # noqa: BLE001
            print(f"  RECORDING FAILED: {type(exc).__name__}: {exc}")
            report.append({"prompt": text, "error": str(exc)})
            continue

        print(
            "  VAD  open {device_open_s:.2f}s | wait {wait_s:.2f}s | speech {speech_s:.2f}s | "
            "end-of-speech {end_of_speech_delay_ms:.0f}ms | shutdown {shutdown_s:.3f}s | "
            "flush {flush_s:.3f}s | total {capture_s:.2f}s | stop={stop_reason}".format(**metrics)
        )
        print(f"  background {metrics.get('background_db', 0):.1f} dBFS | "
              f"speech level {metrics.get('speech_level_db', 0):.1f} dBFS")

        transcript, stt_s = va.transcribe(cfg, str(turn_path))
        print(f"  STT   ({stt_s:.2f}s): {transcript!r}")
        response, n8n_s = va.send_to_n8n(cfg, transcript or "")
        print(f"  N8N   ({n8n_s:.2f}s): {response!r}")

        if args.play and response:
            print("  PLAYING the answer...")
            tts = va.KokoroTTS(cfg)
            tts.warmup()
            tts.synthesize(response, "/tmp/live_mic_answer.wav")
            va.play_audio(cfg, "/tmp/live_mic_answer.wav")
            time.sleep(0.5)

        ok = True
        if expected is not None:
            ok = bool(transcript) and expected in transcript.replace(",", "")
        if transcript is not None and expected is not None:
            print(f"  CHECK order number {expected} in transcript: {'OK' if ok else 'MISMATCH'}")

        report.append({
            "audio": str(turn_path),
            "prompt": text,
            "expected_order": expected,
            "transcript": transcript,
            "response": response,
            "order_number_ok": ok,
            "capture_s": capture_s,
            "stt_s": stt_s,
            "n8n_s": n8n_s,
            "vad": metrics,
        })
        time.sleep(0.3)

    print("-" * 72)
    print("\n  Summary")
    print(f"  {'prompt':<45s} {'capture':>8s} {'stt':>6s} {'n8n':>6s}  order")
    for row in report:
        if "error" in row:
            print(f"  {row['prompt'][:44]:<45s} ERROR {row['error'][:30]}")
            continue
        mark = "-" if row["expected_order"] is None else ("OK" if row["order_number_ok"] else "BAD")
        print(
            f"  {row['prompt'][:44]:<45s} {row['capture_s']:8.2f} {row['stt_s']:6.2f} "
            f"{row['n8n_s']:6.2f}  {mark}"
        )

    out = Path(args.out) if args.out else ROOT / "logs" / f"live_mic_test_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n  Report written to {out}")


if __name__ == "__main__":
    main()
