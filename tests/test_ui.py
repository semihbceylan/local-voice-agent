#!/usr/bin/env python3
"""
Tests for the terminal presentation (ui.py) and the startup checks.

Run:  .venv/bin/python tests/test_ui.py
"""

import io
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import Console  # noqa: E402

ANSI = re.compile(r"\x1b\[")


def plain_console(**kwargs) -> tuple[Console, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    return Console(out=out, err=err, **kwargs), out, err


class ConsoleTests(unittest.TestCase):
    def test_a_pipe_gets_plain_text(self):
        """Not a terminal: no colour, no live line, nothing to strip in a log."""
        ui, out, err = plain_console()
        ui.title("DemoShop Voice Agent")
        ui.check(True, "ffmpeg", "/opt/homebrew/bin/ffmpeg")
        ui.user("Where is my order 1001?")
        ui.listening(-20.0, hearing=True)
        ui.speaking(2.0, barge_in=False)
        with ui.working("Thinking"):
            pass
        ui.error("boom", hint="do this")
        self.assertIsNone(ANSI.search(out.getvalue()), out.getvalue())
        self.assertIsNone(ANSI.search(err.getvalue()), err.getvalue())
        self.assertNotIn("Hearing you", out.getvalue())
        self.assertIn("boom", err.getvalue())
        self.assertIn("do this", err.getvalue())

    def test_live_terminal_draws_and_clears_the_status_line(self):
        ui, out, _ = plain_console(live=True, color=False)
        ui.speaking(2.0, barge_in=True)
        self.assertIn("talk to interrupt", out.getvalue())
        ui.agent("Hello")
        # The status line is cleared before the next message is printed.
        self.assertIn("\r\x1b[2K", out.getvalue())
        self.assertTrue(out.getvalue().rstrip().endswith("Hello"))

    def test_long_answers_are_wrapped_not_truncated(self):
        ui, out, _ = plain_console()
        answer = " ".join(f"word{i}" for i in range(80))
        ui.agent(answer)
        text = out.getvalue()
        self.assertIn("word0", text)
        self.assertIn("word79", text)
        self.assertNotIn("...", text)
        for line in text.splitlines():
            self.assertLessEqual(len(line), ui.width)

    def test_timing_skips_stages_that_did_not_run(self):
        ui, out, _ = plain_console()
        ui.timing({"stt_s": 0.0, "n8n_s": 0.42, "tts_s": 0.0, "post_speech_ttfa_s": 0.42})
        line = out.getvalue()
        self.assertIn("n8n 0.42", line)
        self.assertNotIn("stt", line)
        self.assertNotIn("voice", line)
        self.assertIn("answer in 0.42s", line)

        ui, out, _ = plain_console()
        ui.timing({"stt_s": 0.3, "n8n_s": 0.05, "tts_s": 0.4, "post_speech_ttfa_s": 0.75})
        self.assertIn("first audio in 0.75s", out.getvalue())

    def test_verbose_shows_the_breakdown(self):
        ui, out, _ = plain_console(verbose=True)
        ui.timing({
            "stt_s": 0.3, "n8n_s": 0.05, "tts_s": 0.4, "post_speech_ttfa_s": 0.75,
            "capture_s": 2.0, "playback_s": 3.0, "ttfa_s": 2.75, "total_s": 5.75,
            "vad": {"speech_s": 1.2, "end_of_speech_delay_ms": 810, "stop_reason": "silence"},
        })
        self.assertIn("total 5.75s", out.getvalue())
        self.assertIn("end-of-speech 810 ms", out.getvalue())

    def test_summary_counts_turns(self):
        ui, out, _ = plain_console()
        for ttfa in (0.8, 1.0, 1.4):
            ui.timing({"stt_s": 0.3, "n8n_s": 0.1, "tts_s": 0.4, "post_speech_ttfa_s": ttfa})
        ui.summary("Call ended")
        last = out.getvalue().strip().splitlines()[-1]
        self.assertIn("3 turns", last)
        self.assertIn("first audio median 1.00s", last)
        self.assertIn("worst 1.40s", last)

    def test_meter_is_clamped(self):
        ui, _, _ = plain_console(color=False)
        self.assertEqual(ui._meter(-100.0, cells=10), "▯" * 10)
        self.assertEqual(ui._meter(0.0, cells=10), "▮" * 10)

    def test_no_color_is_respected(self):
        import os
        os.environ["NO_COLOR"] = "1"
        try:
            ui, out, _ = plain_console(live=True)
            ui.user("hi")
            self.assertIsNone(ANSI.search(out.getvalue().replace("\r\x1b[2K", "")))
        finally:
            del os.environ["NO_COLOR"]


class DeviceListingTests(unittest.TestCase):
    LISTING = """\
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] [1] Capture screen 0
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] JBL VIBE BEAM
[AVFoundation indev @ 0x1] [1] MacBook Pro Microphone
[in#0 @ 0x2] Error opening input: Input/output error
"""

    def test_only_audio_devices_are_listed(self):
        import voice_agent as va
        self.assertEqual(
            va.parse_avfoundation_devices(self.LISTING),
            [(0, "JBL VIBE BEAM"), (1, "MacBook Pro Microphone")],
        )

    def test_no_audio_devices(self):
        import voice_agent as va
        listing = self.LISTING.split("AVFoundation audio devices")[0]
        self.assertEqual(va.parse_avfoundation_devices(listing), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
