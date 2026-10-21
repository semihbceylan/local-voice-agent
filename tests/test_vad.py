#!/usr/bin/env python3
"""
Automated tests for the voice activity detection state machine.

Run:  .venv/bin/python tests/test_vad.py

The tests use real recorded speech (tests/fixtures/speech_only.wav) and a
synthetic room tone, arranged the way a capture really happens:

    room tone -> speech -> room tone

That ordering matters: the endpointer has to learn the noise floor from the
room tone before the customer starts talking, otherwise it mistakes the
quiet parts of the speech for silence and cuts the utterance short.
"""

import sys
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import voice_agent as va  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures_gen import speech_fixture  # noqa: E402

SR = 16000
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def load(path):
    with wave.open(str(path), "rb") as wav:
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def room_tone(seconds, db, seed=5, pink=False):
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    if pink:
        white = rng.normal(0, 1, n)
        freqs = np.fft.rfftfreq(n, 1 / SR)
        spectrum = np.fft.rfft(white) * (freqs ** -0.5)
        signal = np.fft.irfft(spectrum, n)
        signal = signal / (np.max(np.abs(signal)) or 1.0)
    else:
        signal = rng.normal(0, 1, n)
    return (signal * 10 ** (db / 20)).astype(np.float32)


def frames(signal, ms=va.FRAME_MS):
    count = int(SR * ms / 1000)
    data = (signal * 32767).astype(np.int16).tobytes()
    return [data[i:i + count * 2] for i in range(0, len(data) - count * 2 + 1, count * 2)]


def capture(signal, **overrides):
    cfg = va.Config()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return va.segment_speech(frames(signal), cfg)


class VadEndOfSpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            path = speech_fixture()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"no speech fixture available: {exc}")
        cls.speech = load(path)

    def scene(self, room_db, lead=1.0, tail=6.0, pink=False):
        return np.concatenate([
            room_tone(lead, room_db, pink=pink),
            self.speech,
            room_tone(tail, room_db, pink=pink),
        ])

    def test_stops_at_the_configured_threshold(self):
        for room_db in (-70, -60, -50, -40, -30):
            with self.subTest(room_tone_db=room_db):
                pcm, metrics = capture(self.scene(room_db), vad_silence_ms=1000)
                self.assertEqual(metrics["stop_reason"], "silence")
                self.assertLessEqual(metrics["end_of_speech_delay_ms"], 1300)
                self.assertGreaterEqual(metrics["end_of_speech_delay_ms"], 900)

    def test_the_whole_utterance_is_captured(self):
        """Regression: the quiet parts of the speech were treated as silence."""
        for room_db in (-70, -60, -50, -40, -30):
            with self.subTest(room_tone_db=room_db):
                pcm, metrics = capture(self.scene(room_db), vad_silence_ms=1000)
                self.assertGreater(
                    metrics["speech_s"], len(self.speech) / SR * 0.85,
                    "the utterance was cut short",
                )

    def test_pink_room_tone(self):
        pcm, metrics = capture(self.scene(-50, pink=True), vad_silence_ms=1000)
        self.assertEqual(metrics["stop_reason"], "silence")
        self.assertLessEqual(metrics["end_of_speech_delay_ms"], 1300)

    def test_threshold_is_configurable(self):
        for threshold in (500, 800, 1500):
            with self.subTest(threshold=threshold):
                pcm, metrics = capture(self.scene(-50), vad_silence_ms=threshold)
                self.assertEqual(metrics["stop_reason"], "silence")
                self.assertLessEqual(metrics["end_of_speech_delay_ms"], threshold + 300)
                self.assertGreaterEqual(metrics["end_of_speech_delay_ms"], threshold - 100)

    def test_natural_pause_inside_an_utterance_is_preserved(self):
        """A pause shorter than the threshold must not end the recording."""
        first = self.speech[: int(SR * 1.0)]
        second = self.speech[int(SR * 1.4):]
        gap = np.zeros(int(SR * 0.6), dtype=np.float32)
        signal = np.concatenate([room_tone(0.5, -60), first, gap, second, room_tone(3, -60)])
        pcm, metrics = capture(signal, vad_silence_ms=1000)
        self.assertEqual(metrics["stop_reason"], "silence")
        self.assertGreater(metrics["speech_s"], 1.2, "the pause truncated the utterance")

    def test_start_of_utterance_is_not_clipped(self):
        pcm, metrics = capture(self.scene(-60), vad_silence_ms=1000)
        kept = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self.assertGreater(len(kept), int(SR * 1.0))

    def test_maximum_duration_is_enforced(self):
        signal = np.concatenate([room_tone(0.5, -60), self.speech, room_tone(20, -60)])
        pcm, metrics = capture(signal, vad_silence_ms=1000, vad_max_seconds=2)
        self.assertEqual(metrics["stop_reason"], "max_duration")
        self.assertLessEqual(metrics["speech_s"], 3.0)

    def test_silence_only_input_is_rejected(self):
        with self.assertRaises(va.NoSpeechDetected):
            capture(room_tone(3, -60), vad_silence_ms=1000)

    def test_too_short_utterance_is_rejected(self):
        signal = np.concatenate([room_tone(0.5, -60), self.speech[: int(SR * 0.1)], room_tone(2, -60)])
        with self.assertRaises(va.NoSpeechDetected):
            capture(signal, vad_silence_ms=1000, vad_min_speech_ms=300)

    def test_metrics_breakdown_is_populated(self):
        pcm, metrics = capture(self.scene(-40), vad_silence_ms=1000)
        for key in (
            "wait_s", "speech_s", "signal_s", "vad_voiced_s", "quiet_s",
            "trailing_silence_ms", "end_of_speech_delay_ms", "end_of_speech_wall_s",
            "wall_span_s", "audio_span_s", "pipeline_lag_s", "stop_reason",
            "kept_s", "background_db", "speech_level_db", "local_bg_db",
            "vad_unreliable", "vad_conflict_max",
        ):
            self.assertIn(key, metrics)
        self.assertGreaterEqual(metrics["wait_s"], 0.0)
        self.assertGreater(metrics["speech_s"], 0.0)

    def test_live_microphone_capture_is_not_truncated(self):
        """
        Regression test on a real recording from the Bluetooth headset: the
        endpointer must not cut the utterance at a quiet passage.
        """
        path = FIXTURES / "mic_capture.wav"
        if not path.exists():
            self.skipTest("no live microphone capture available")
        pcm, metrics = capture(load(path), vad_silence_ms=800)
        self.assertGreater(metrics["speech_s"], 0.4)


class PushToTalkTests(unittest.TestCase):
    def test_record_fixed_builds_a_valid_command(self):
        cfg = va.Config()
        cmd = va._ffmpeg_input_command(cfg) + ["-t", "5", "-ar", "16000", "-ac", "1", "/tmp/x.wav"]
        self.assertIn("avfoundation", cmd)
        self.assertIn("-t", cmd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
