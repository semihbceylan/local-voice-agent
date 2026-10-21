#!/usr/bin/env python3
"""
Shared helpers for the test suite.

The speech fixture is generated locally with Kokoro instead of being shipped
in the repository: no recording of a real microphone is published, and the
tests still run on a fresh clone.
"""

import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SPEECH_FIXTURE = FIXTURES / "speech_only.wav"
KOKORO_REPO = "mlx-community/Kokoro-82M-bf16"
KOKORO_VOICE = "af_heart"
KOKORO_LANG = "a"
SPEECH_TEXT = "Where is my order one thousand and one, please?"


def speech_fixture() -> Path:
    """
    Return a 16 kHz mono speech fixture, generating it with Kokoro if needed.
    """
    if SPEECH_FIXTURE.exists():
        return SPEECH_FIXTURE
    FIXTURES.mkdir(parents=True, exist_ok=True)
    try:
        import soundfile as sf
        from mlx_audio.tts import utils
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Kokoro is not available, so the speech fixture cannot be created"
        ) from exc

    model = utils.load(KOKORO_REPO)
    segments = list(
        model.generate(SPEECH_TEXT, voice=KOKORO_VOICE, lang_code=KOKORO_LANG)
    )
    audio = np.concatenate([np.array(segment.audio) for segment in segments])
    rate = int(segments[0].sample_rate)

    raw = SPEECH_FIXTURE.with_suffix(".raw.wav")
    sf.write(str(raw), audio, rate)
    converted = SPEECH_FIXTURE.with_suffix(".converted.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
         "-ar", "16000", "-ac", "1", str(converted)],
        check=True,
    )
    raw.unlink(missing_ok=True)

    # Trim the silence around the sentence, so the fixture is pure speech.
    import wave

    with wave.open(str(converted), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    block = 480  # 30 ms
    count = len(samples) // block
    levels = [
        20 * np.log10(max(float(np.sqrt(np.mean(samples[i * block:(i + 1) * block] ** 2))), 1e-6))
        for i in range(count)
    ]
    peak = max(levels)
    loud = [i for i, level in enumerate(levels) if level > peak - 25.0]
    if loud:
        start = max(0, loud[0] - 3) * block
        end = min(len(samples), (loud[-1] + 4) * block)
        samples = samples[start:end]

    # A synthesised voice is much steadier and quieter on average than a
    # real one. Scale it to a realistic close microphone level (peak around
    # -6 dBFS; the live headset capture peaked at -9 dBFS), otherwise the
    # quiet parts of the sentence would be quieter than a real microphone
    # ever produces and the endpointer would treat them as background.
    peak_level = 20 * np.log10(max(float(np.max(np.abs(samples))), 1e-6))
    if peak_level > -100:
        samples = samples * (10 ** ((-6.0 - peak_level) / 20.0))

    with wave.open(str(SPEECH_FIXTURE), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes((samples * 32767).astype(np.int16).tobytes())
    converted.unlink(missing_ok=True)
    return SPEECH_FIXTURE


def main() -> int:
    try:
        path = speech_fixture()
    except Exception as exc:  # noqa: BLE001
        print(f"could not create the fixture: {exc}", file=sys.stderr)
        return 1
    print(f"speech fixture ready: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
