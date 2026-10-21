#!/usr/bin/env python3
"""
Analyse a raw microphone sample: level profile, VAD behaviour and how the
current endpointer reacts to it.

Usage:
    .venv/bin/python tests/analyse_sample.py tests/fixtures/mic_sample.wav
"""

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import voice_agent as va  # noqa: E402

SR = 16000


def frame_levels(frames):
    out = []
    for frame in frames:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples)))
        out.append(20 * np.log10(rms) if rms > 1e-7 else -100.0)
    return out


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/mic_sample.wav")
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    frame_bytes = int(SR * va.FRAME_MS / 1000) * 2
    frames = [pcm[i:i + frame_bytes] for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes)]
    levels = frame_levels(frames)

    print(f"file      : {path}  ({len(pcm) / 2 / rate:.2f}s, {len(frames)} frames)")
    print(f"level p10 : {np.percentile(levels, 10):.1f} dBFS")
    print(f"level p50 : {np.percentile(levels, 50):.1f} dBFS")
    print(f"level p90 : {np.percentile(levels, 90):.1f} dBFS")
    print(f"level max : {max(levels):.1f} dBFS")
    print()

    import webrtcvad

    for aggr in (2, 3):
        vad = webrtcvad.Vad(aggr)
        flags = "".join("1" if vad.is_speech(f, SR) else "0" for f in frames)
        print(f"VAD aggr{aggr}: {flags}")
    print()

    smoothed = []
    current = None
    for value in levels:
        current = value if current is None else current + 0.4 * (value - current)
        smoothed.append(current)
    print("level profile (dBFS per 30ms frame):")
    for start in range(0, len(levels), 25):
        chunk = levels[start:start + 25]
        print(f"  {start * 0.03:5.2f}s " + " ".join(f"{v:5.0f}" for v in chunk))
    print()

    for threshold in (600, 800, 1000):
        cfg = va.Config()
        cfg.vad_silence_ms = threshold
        try:
            audio, metrics = va.segment_speech(frames, cfg)
            print(
                f"current endpointer (silence {threshold}ms): kept {metrics['kept_s']:.2f}s "
                f"speech {metrics['speech_s']:.2f}s stop={metrics['stop_reason']} "
                f"delay={metrics['end_of_speech_delay_ms']}ms bg={metrics['background_db']:.1f} "
                f"signal_frames={metrics['signal_frames']}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"current endpointer (silence {threshold}ms): {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
