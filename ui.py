"""
Terminal presentation for the voice agent.

Everything the client shows the person in front of the terminal goes through
the Console below, so the pipeline code stays free of formatting:

    You    Where is my order 1001?
    Agent  Your order #1001 shipped on Monday.
           stt 0.31 · n8n 0.05 · voice 0.42  ->  first audio 0.78s

On an interactive terminal there is also one live status line (listening
with a microphone level meter, transcribing, thinking, speaking). When the
output is not a terminal (a pipe, a log file, the test suite) the live line
is skipped and the output stays plain text. Colour follows the NO_COLOR
convention (https://no-color.org).
"""

import os
import shutil
import statistics
import sys
import textwrap
import threading
import time
from typing import Optional, TextIO


def _supports_live(stream: TextIO) -> bool:
    try:
        is_tty = stream.isatty()
    except (AttributeError, ValueError):
        return False
    return is_tty and os.environ.get("TERM", "") != "dumb"


class _Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def cyan(self, t: str) -> str:
        return self._wrap("1;36", t)

    def green(self, t: str) -> str:
        return self._wrap("1;32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def red(self, t: str) -> str:
        return self._wrap("1;31", t)

    def magenta(self, t: str) -> str:
        return self._wrap("35", t)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
LABEL_WIDTH = 7  # "Agent  "


class Console:
    def __init__(
        self,
        out: TextIO = sys.stdout,
        err: TextIO = sys.stderr,
        color: Optional[bool] = None,
        live: Optional[bool] = None,
        verbose: bool = False,
    ):
        self.out = out
        self.err = err
        self.live = _supports_live(out) if live is None else live
        if color is None:
            color = self.live and "NO_COLOR" not in os.environ
        self.s = _Style(color)
        self.verbose = verbose
        self._lock = threading.Lock()
        self._status_visible = False
        self._last_level_draw = 0.0
        self._turns: list[dict] = []
        self._started_at = time.monotonic()

    # ------------------------------------------------------------------ #
    # Low level
    # ------------------------------------------------------------------ #

    @property
    def width(self) -> int:
        return max(40, min(100, shutil.get_terminal_size((80, 24)).columns))

    def _clear_status(self) -> None:
        if self._status_visible:
            self.out.write("\r\033[2K")
            self._status_visible = False

    def _println(self, text: str = "", stream: Optional[TextIO] = None) -> None:
        with self._lock:
            self._clear_status()
            (stream or self.out).write(text + "\n")
            (stream or self.out).flush()

    def _status(self, text: str) -> None:
        if not self.live:
            return
        with self._lock:
            self.out.write("\r\033[2K" + text)
            self.out.flush()
            self._status_visible = True

    def clear_status(self) -> None:
        with self._lock:
            self._clear_status()
            self.out.flush()

    # ------------------------------------------------------------------ #
    # Header and checks
    # ------------------------------------------------------------------ #

    def title(self, text: str) -> None:
        self._println()
        self._println("  " + self.s.bold(text))
        self._println("  " + self.s.dim("─" * min(len(text) + 4, self.width - 4)))

    def check(self, ok: Optional[bool], name: str, detail: str = "", hint: str = "") -> None:
        """One line of the startup checklist. ok=None is a warning."""
        if ok is True:
            mark = self.s.green("✓")
        elif ok is None:
            mark = self.s.yellow("!")
        else:
            mark = self.s.red("✗")
        self._println(f"  {mark} {name:<12} {self.s.dim(detail) if ok else detail}")
        if hint and ok is not True:
            for line in textwrap.wrap(hint, self.width - 17):
                self._println(" " * 17 + self.s.dim(line))

    def facts(self, items: list[tuple[str, str]]) -> None:
        """A compact 'key value · key value' line under the checklist."""
        text = " · ".join(f"{k} {v}" for k, v in items)
        for line in textwrap.wrap(text, self.width - 4):
            self._println("  " + self.s.dim(line))

    def hint(self, text: str) -> None:
        self._println("  " + self.s.dim(text))

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #

    def info(self, text: str) -> None:
        self._println("  " + self.s.dim(text))

    def warn(self, text: str, hint: str = "") -> None:
        self._println("  " + self.s.yellow("! " + text), self.err)
        if hint:
            self._println("    " + self.s.dim(hint), self.err)

    def error(self, text: str, hint: str = "") -> None:
        self._println("  " + self.s.red("✗ ") + text, self.err)
        if hint:
            for line in textwrap.wrap(hint, self.width - 6):
                self._println("    " + self.s.dim(line), self.err)

    def debug(self, text: str) -> None:
        if self.verbose:
            self._println("  " + self.s.dim(text))

    # ------------------------------------------------------------------ #
    # Conversation
    # ------------------------------------------------------------------ #

    def _message(self, label: str, colored_label: str, text: str) -> None:
        body_width = max(20, self.width - 2 - LABEL_WIDTH)
        lines = textwrap.wrap(text, body_width) or [""]
        pad = " " * (LABEL_WIDTH - len(label))
        self._println()
        self._println(f"  {colored_label}{pad}{lines[0]}")
        for line in lines[1:]:
            self._println("  " + " " * LABEL_WIDTH + line)

    def user(self, text: str) -> None:
        self._message("You", self.s.cyan("You"), text)

    def agent(self, text: str) -> None:
        self._message("Agent", self.s.green("Agent"), text)

    def note(self, text: str) -> None:
        """A small remark under the last message (e.g. 'not understood')."""
        self._println("  " + " " * LABEL_WIDTH + self.s.dim(text))

    def timing(self, rows: dict) -> None:
        """Remember the turn and print its timing under the answer."""
        self._turns.append(rows)
        stages = [("stt", "stt_s"), ("n8n", "n8n_s"), ("voice", "tts_s")]
        # A stage that did not run in this mode (text mode, --no-play) is 0.
        parts = [f"{name} {rows[key]:.2f}" for name, key in stages if rows.get(key)]
        ttfa = rows.get("post_speech_ttfa_s", 0.0)
        result = "first audio" if rows.get("tts_s") else "answer"
        line = " · ".join(parts) + f"  →  {result} in {ttfa:.2f}s"
        if rows.get("interrupted"):
            line += "  (interrupted)"
        self.note(line)
        if self.verbose:
            self.note(
                f"capture {rows.get('capture_s', 0):.2f}s · "
                f"playback {rows.get('playback_s', 0):.2f}s · "
                f"from start of capture {rows.get('ttfa_s', 0):.2f}s · "
                f"total {rows.get('total_s', 0):.2f}s"
            )
            vad = rows.get("vad") or {}
            if vad:
                self.note(
                    f"vad: speech {vad.get('speech_s', 0):.2f}s · "
                    f"end-of-speech {vad.get('end_of_speech_delay_ms', 0)} ms · "
                    f"stop {vad.get('stop_reason', '?')}"
                )

    # ------------------------------------------------------------------ #
    # Live status line
    # ------------------------------------------------------------------ #

    def _meter(self, level_db: float, cells: int = 14) -> str:
        # -65 dBFS (room tone on the measured headset) .. -10 dBFS (loud speech)
        fill = (level_db + 65.0) / 55.0
        n = int(round(max(0.0, min(1.0, fill)) * cells))
        return self.s.green("▮" * n) + self.s.dim("▯" * (cells - n))

    def listening(self, level_db: float, hearing: bool, hint: str = "") -> None:
        """Redraw the listening line; rate limited, cheap to call per frame."""
        if not self.live:
            return
        now = time.monotonic()
        if now - self._last_level_draw < 0.08:
            return
        self._last_level_draw = now
        state = self.s.cyan("● Hearing you") if hearing else self.s.green("● Listening ")
        tail = f"  {self.s.dim(hint)}" if hint else ""
        self._status(f"  {state}  {self._meter(level_db)}{tail}")

    def speaking(self, seconds: float, barge_in: bool) -> None:
        hint = "talk to interrupt" if barge_in else "Ctrl-C to hang up"
        self._status(
            f"  {self.s.magenta('♪ Speaking')}  {self.s.dim(f'{seconds:.1f}s · {hint}')}"
        )

    def working(self, label: str) -> "_Spinner":
        """`with console.working("Thinking"):` shows a spinner on a terminal."""
        return _Spinner(self, label)

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #

    def summary(self, reason: str = "Call ended") -> None:
        elapsed = time.monotonic() - self._started_at
        minutes, seconds = divmod(int(elapsed), 60)
        parts = [reason, f"{len(self._turns)} turn{'s' if len(self._turns) != 1 else ''}",
                 f"{minutes}m{seconds:02d}s"]
        ttfa = [t["post_speech_ttfa_s"] for t in self._turns if "post_speech_ttfa_s" in t]
        if ttfa:
            spoken = any(t.get("tts_s") for t in self._turns)
            label = "first audio" if spoken else "answer"
            parts.append(f"{label} median {statistics.median(ttfa):.2f}s")
            if len(ttfa) >= 2:
                parts.append(f"worst {max(ttfa):.2f}s")
        self._println()
        self._println("  " + self.s.bold(parts[0]) + self.s.dim(" · " + " · ".join(parts[1:])))


class _Spinner:
    def __init__(self, console: Console, label: str):
        self.console = console
        self.label = label
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        start = time.monotonic()
        i = 0
        while not self._stop.wait(0.08):
            frame = SPINNER[i % len(SPINNER)]
            elapsed = time.monotonic() - start
            self.console._status(
                f"  {self.console.s.yellow(frame + ' ' + self.label)}"
                f"{self.console.s.dim(f'  {elapsed:.1f}s')}"
            )
            i += 1

    def __enter__(self) -> "_Spinner":
        if self.console.live:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self.console.clear_status()
