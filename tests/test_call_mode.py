#!/usr/bin/env python3
"""
Tests for the continuous call mode.

The call loop is exercised with a scripted frame source, so the whole
"listen -> turn -> answer -> listen again" path runs without a microphone.

Run:  .venv/bin/python tests/test_call_mode.py
"""

import shutil
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import voice_agent as va  # noqa: E402
from vad_stream import VadStream  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures_gen import speech_fixture  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def make_wav(path: Path, seconds: float, db: float = -70.0, seed: int = 3) -> None:
    """A file with room tone only, used to test the idle behaviour."""
    rng = np.random.default_rng(seed)
    n = int(16000 * seconds)
    signal = (rng.normal(0, 10 ** (db / 20), n)).astype(np.float32)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes((signal * 32767).astype(np.int16).tobytes())


class CallModeTests(unittest.TestCase):
    def test_vad_stream_produces_turn_after_turn(self):
        """The same stream must deliver one utterance after the other."""
        cfg = va.Config()
        cfg.vad_silence_ms = 500
        stream = VadStream(cfg)
        speech = FIXTURES / "speech_only.wav"
        with wave.open(str(speech), "rb") as wav:
            data = wav.readframes(wav.getnframes())
        frame_bytes = int(16000 * va.FRAME_MS / 1000) * 2
        frames = [data[i:i + frame_bytes] for i in range(0, len(data) - frame_bytes + 1, frame_bytes)]
        silence = b"\x00\x00" * (16000 * 3)  # 3 s of digital silence

        utterances = []
        for frame in frames:
            result = stream.push(frame)
            if result is not None:
                utterances.append(result)
        for index in range(0, len(silence), frame_bytes):
            result = stream.push(silence[index:index + frame_bytes])
            if result is not None:
                utterances.append(result)

        self.assertEqual(len(utterances), 1, "the speech was not detected as one turn")
        self.assertGreater(len(utterances[0]), frame_bytes * 10)

    def test_stream_keeps_working_after_a_turn(self):
        cfg = va.Config()
        cfg.vad_silence_ms = 500
        stream = VadStream(cfg)
        with wave.open(str(FIXTURES / "speech_only.wav"), "rb") as wav:
            data = wav.readframes(wav.getnframes())
        frame_bytes = int(16000 * va.FRAME_MS / 1000) * 2
        frames = [data[i:i + frame_bytes] for i in range(0, len(data) - frame_bytes + 1, frame_bytes)]
        silence = b"\x00\x00" * (16000 * 2)

        turns = 0
        for _round in range(2):
            for frame in frames:
                if stream.push(frame) is not None:
                    turns += 1
            for index in range(0, len(silence), frame_bytes):
                if stream.push(silence[index:index + frame_bytes]) is not None:
                    turns += 1
        self.assertEqual(turns, 2, "the second utterance was not detected")

    def test_idle_room_tone_produces_no_turn(self):
        cfg = va.Config()
        cfg.vad_silence_ms = 500
        stream = VadStream(cfg)
        rng = np.random.default_rng(1)
        signal = (rng.normal(0, 10 ** (-60 / 20), int(16000 * 3))).astype(np.float32)
        data = (signal * 32767).astype(np.int16).tobytes()
        frame_bytes = int(16000 * va.FRAME_MS / 1000) * 2
        turns = 0
        for index in range(0, len(data) - frame_bytes + 1, frame_bytes):
            if stream.push(data[index:index + frame_bytes]) is not None:
                turns += 1
        self.assertEqual(turns, 0, "room tone produced a turn")

    def test_wav_frame_source_delivers_frames(self):
        cfg = va.Config()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            make_wav(path, 1.0)
            source = va.WavFrameSource(cfg, str(path))
            source.start()
            frames = []
            while True:
                frame = source.read(timeout=0.1)
                if frame is None:
                    break
                frames.append(frame)
            self.assertGreater(len(frames), 20)
            self.assertEqual(len(frames[0]), int(16000 * va.FRAME_MS / 1000) * 2)

    def test_call_loop_answers_and_keeps_listening(self):
        """The full loop: listen -> transcribe -> answer -> listen again."""
        cfg = va.Config()
        cfg.vad_silence_ms = 500
        cfg.webhook_url = "http://localhost:5678/webhook/voice-agent"
        cfg.session_id = "call-test"

        calls = []

        def fake_transcribe(cfg, path):
            calls.append(("stt", path))
            if len([kind for kind, _ in calls if kind == "stt"]) >= 2:
                raise KeyboardInterrupt  # hang up after two turns
            return "Where is my order 1001?", 0.2

        def fake_n8n(cfg, text):
            calls.append(("n8n", text))
            return "Order 1001 has shipped.", 0.05

        def fake_play(cfg, path):
            calls.append(("play", path))
            return True, 0.5

        va.transcribe = fake_transcribe
        va.send_to_n8n = fake_n8n
        va.play_audio = fake_play

        source = va.WavFrameSource(cfg, str(speech_fixture()), loop=True, realtime=False)

        # Stop after two turns.
        original = va.KokoroTTS.synthesize

        def fake_synth(self, text, path):
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x00\x00" * 1600)
            return True, 0.1, 0.1

        va.KokoroTTS.synthesize = fake_synth
        try:
            va.run_call(cfg, source=source, play=True)
        except KeyboardInterrupt:
            pass
        finally:
            va.KokoroTTS.synthesize = original

        kinds = [kind for kind, _ in calls]
        self.assertIn("stt", kinds)
        self.assertIn("n8n", kinds)
        self.assertIn("play", kinds)
        self.assertEqual(kinds.index("stt") < kinds.index("n8n"), True)


    def test_call_ends_on_a_goodbye(self):
        """A farewell must close the call without waking the language model."""
        cfg = va.Config()
        cfg.vad_silence_ms = 500
        cfg.farewell_enabled = True
        cfg.session_id = "farewell-test"

        seen = []

        def fake_transcribe(cfg, path):
            return "have a nice day", 0.2

        def fake_n8n(cfg, text):
            seen.append(text)
            return "You are welcome.", 0.05

        def fake_play(cfg, path):
            return True, 0.3

        va.transcribe = fake_transcribe
        va.send_to_n8n = fake_n8n
        va.play_audio = fake_play

        def fake_synth(self, text, path):
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x00\x00" * 1600)
            return True, 0.05, 0.05

        original = va.KokoroTTS.synthesize
        va.KokoroTTS.synthesize = fake_synth
        try:
            source = va.WavFrameSource(
                cfg, str(speech_fixture()), loop=True, realtime=False
            )
            va.run_call(cfg, source=source, play=True)
        finally:
            va.KokoroTTS.synthesize = original

        self.assertEqual(seen, [], "the farewell must not reach the backend")


if __name__ == "__main__":
    unittest.main(verbosity=2)
