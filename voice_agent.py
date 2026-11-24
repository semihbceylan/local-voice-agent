#!/usr/bin/env python3
"""
Fully local voice customer-support client.

Pipeline:
    microphone -> ffmpeg/avfoundation -> (WebRTC VAD) -> whisper.cpp
    -> n8n voice-agent webhook -> Kokoro TTS (loaded once) -> afplay

Modes:
    call  Always listening, the agent answers by itself (default).
    arm   Press Enter, then speak; voice activity detection ends the turn.
    ptt   Fixed-duration push-to-talk, used as a fallback.
    text  Type the questions instead of speaking them.

Configuration is via CLI flags or environment variables. No secrets are used
or stored by this client.
"""

import argparse
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional, Tuple
from urllib.parse import urlparse

import numpy as np
import requests

from farewell import farewell_response, is_farewell
from ui import Console
from vad_stream import (
    FRAME_MS,
    NoSpeechDetected,
    VadStream,
    _level_db,
)

BASE_DIR = Path(__file__).resolve().parent

# Everything the person sees goes through this console. main() replaces it
# with one configured from the command line.
UI = Console()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    webhook_url: str = _env("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/voice-agent")
    whisper_cli: str = _env("WHISPER_CLI", "/opt/homebrew/bin/whisper-cli")
    whisper_model: str = _env(
        "WHISPER_MODEL", str(BASE_DIR / "ggml-base.en.bin")
    )
    # Optional decoding hint for whisper.cpp. A prompt that mentions a
    # specific order number can fix a misheard digit, but it can also bias
    # other numbers, so it stays off by default.
    whisper_prompt: str = _env("WHISPER_PROMPT", "")
    kokoro_repo: str = _env("KOKORO_REPO", "mlx-community/Kokoro-82M-bf16")
    kokoro_voice: str = _env("KOKORO_VOICE", "af_heart")
    kokoro_lang: str = _env("KOKORO_LANG", "a")
    kokoro_speed: float = _env_float("KOKORO_SPEED", 1.0)

    audio_device: str = _env("AUDIO_DEVICE", ":0")  # AVFoundation: JBL VIBE BEAM
    sample_rate: int = _env_int("AUDIO_SAMPLE_RATE", 16000)
    channels: int = _env_int("AUDIO_CHANNELS", 1)

    ptt_duration: float = _env_float("PTT_DURATION", 5.0)
    vad_silence_ms: int = _env_int("VAD_SILENCE_MS", 800)
    vad_max_seconds: float = _env_float("VAD_MAX_SECONDS", 15.0)
    vad_min_speech_ms: int = _env_int("VAD_MIN_SPEECH_MS", 300)
    vad_aggressiveness: int = _env_int("VAD_AGGRESSIVENESS", 3)
    vad_preroll_ms: int = _env_int("VAD_PREROLL_MS", 300)
    vad_no_speech_timeout_s: float = _env_float("VAD_NO_SPEECH_TIMEOUT_S", 8.0)

    # End-of-speech detection tuning. WebRTC VAD alone reports steady room
    # tone as speech above roughly -30 dBFS, which prevents the silence
    # counter from ever reaching the threshold. The energy gate below keeps
    # the background out of the speech decision.
    vad_noise_margin_db: float = _env_float("VAD_NOISE_MARGIN_DB", 6.0)
    vad_snr_gap_db: float = _env_float("VAD_SNR_GAP_DB", 12.0)
    barge_in: bool = _env("BARGE_IN", "0") not in ("0", "false", "False", "")
    barge_in_db: float = _env_float("BARGE_IN_DB", 8.0)
    barge_in_ms: int = _env_int("BARGE_IN_MS", 250)
    farewell_enabled: bool = _env("FAREWELL_ENABLED", "1") not in ("0", "false", "False", "")
    farewell_line: str = _env("FAREWELL_LINE", "")
    vad_ema_alpha: float = _env_float("VAD_EMA_ALPHA", 0.4)
    vad_flatness_db: float = _env_float("VAD_FLATNESS_DB", 2.0)
    vad_reset_frames: int = _env_int("VAD_RESET_FRAMES", 30)
    vad_conflict_frames: int = _env_int("VAD_CONFLICT_FRAMES", 20)
    vad_background_band_db: float = _env_float("VAD_BACKGROUND_BAND_DB", 12.0)
    vad_snr_arm_db: float = _env_float("VAD_SNR_ARM_DB", 20.0)
    vad_stuck_flat_db: float = _env_float("VAD_STUCK_FLAT_DB", 2.5)
    vad_start_frames: int = _env_int("VAD_START_FRAMES", 3)
    vad_keep_trailing_silence_ms: int = _env_int("VAD_KEEP_TRAILING_SILENCE_MS", 200)

    audio_input_format: str = _env("AUDIO_INPUT_FORMAT", "avfoundation")

    n8n_timeout_s: float = _env_float("N8N_TIMEOUT_S", 60.0)
    playback_timeout_s: float = _env_float("PLAYBACK_TIMEOUT_S", 120.0)
    latency_log: Path = Path(
        _env("LATENCY_LOG", str(BASE_DIR / "logs" / "latency.jsonl"))
    )

    ffmpeg: str = _env("FFMPEG_CLI", "ffmpeg")
    afplay: str = _env("AFPLAY_CLI", "afplay")
    # Optional output device for the spoken answer. Empty means "use the
    # macOS default output device" (afplay).
    output_device: str = _env("OUTPUT_DEVICE", "")

    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


class RecordingError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Audio capture
# --------------------------------------------------------------------------- #

def _ffmpeg_input_command(cfg: Config) -> list[str]:
    return [
        cfg.ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-f", cfg.audio_input_format,
        "-i", cfg.audio_device,
    ]


def record_fixed(cfg: Config, output_path: str, duration: float) -> Tuple[bool, float]:
    """Push-to-talk fallback: record a fixed number of seconds."""
    start = time.perf_counter()
    cmd = _ffmpeg_input_command(cfg) + [
        "-t", str(duration),
        "-ar", str(cfg.sample_rate),
        "-ac", str(cfg.channels),
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 10)
        elapsed = time.perf_counter() - start
        if result.returncode != 0:
            raise RecordingError(result.stderr.strip() or "ffmpeg recording failed")
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RecordingError("recording produced an empty file")
        return True, elapsed
    except subprocess.TimeoutExpired as exc:
        raise RecordingError("ffmpeg recording timed out") from exc


def _iter_ffmpeg_frames(proc: subprocess.Popen, frame_bytes: int) -> Iterator[bytes]:
    """Yield exact 30 ms PCM frames from ffmpeg's stdout."""
    assert proc.stdout is not None
    while True:
        frame = proc.stdout.read(frame_bytes)
        if not frame:
            return
        if len(frame) == frame_bytes:
            yield frame


def segment_speech(
    frames: Iterable[bytes],
    cfg: Config,
    vad=None,
    clock=time.perf_counter,
) -> Tuple[bytes, dict]:
    """
    Consume PCM frames until speech starts and then stops.

    Returns (pcm_bytes, metrics). Raises NoSpeechDetected when no speech is
    found, when it is too short, or when the no-speech timeout expires.

    This is the single recording wrapper around the shared state machine in
    vad_stream.VadStream, which the continuous call mode also uses.
    """
    stream = VadStream(cfg, vad=vad, clock=clock)
    for frame in frames:
        utterance = stream.push(frame)
        if utterance is not None:
            metrics = stream.last_metrics
            confirmed_ms = metrics["vad_voiced_frames"] * FRAME_MS
            if confirmed_ms < cfg.vad_min_speech_ms:
                raise NoSpeechDetected(f"speech too short ({confirmed_ms} ms)")
            return utterance, metrics
    return stream.flush()


def record_vad(cfg: Config, output_path: str) -> Tuple[bool, float, dict]:
    """Record until the end of speech is detected, with a timing breakdown."""
    start = time.perf_counter()
    cmd = _ffmpeg_input_command(cfg) + [
        "-ar", str(cfg.sample_rate),
        "-ac", str(cfg.channels),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    metrics: dict = {}
    first_frame_at: list[float] = []

    def timed_frames():
        for frame in _iter_ffmpeg_frames(proc, frame_bytes):
            if not first_frame_at:
                first_frame_at.append(time.perf_counter())
            yield frame

    try:
        frame_bytes = int(cfg.sample_rate * FRAME_MS / 1000) * 2 * cfg.channels
        try:
            pcm, metrics = segment_speech(timed_frames(), cfg)
        except NoSpeechDetected:
            if proc.poll() not in (None, 0):
                stderr = (proc.stderr.read() or b"").decode(errors="replace").strip()
                raise RecordingError(stderr or "ffmpeg could not open the microphone")
            raise
    finally:
        shutdown_start = time.perf_counter()
        # Close the read end first: ffmpeg blocks writing into the pipe and
        # cannot handle SIGTERM while it does, which used to cost a full
        # shutdown timeout on every capture.
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        metrics["shutdown_s"] = time.perf_counter() - shutdown_start

    flush_start = time.perf_counter()
    write_wav(output_path, pcm, cfg.sample_rate, cfg.channels)
    metrics["flush_s"] = time.perf_counter() - flush_start
    elapsed = time.perf_counter() - start
    metrics["capture_s"] = elapsed
    metrics["device_open_s"] = (first_frame_at[0] - start) if first_frame_at else elapsed
    metrics["accounted_s"] = (
        metrics["device_open_s"]
        + metrics.get("wait_s", 0.0)
        + metrics.get("speech_s", 0.0)
        + metrics.get("end_of_speech_wall_s", 0.0)
        + metrics.get("shutdown_s", 0.0)
        + metrics.get("flush_s", 0.0)
    )
    return True, elapsed, metrics


def write_wav(path: str, pcm: bytes, sample_rate: int, channels: int) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


# --------------------------------------------------------------------------- #
# Speech-to-text
# --------------------------------------------------------------------------- #

def transcribe(cfg: Config, audio_path: str) -> Tuple[Optional[str], float]:
    start = time.perf_counter()
    cmd = [
        cfg.whisper_cli,
        "-m", cfg.whisper_model,
        "-f", audio_path,
        "-otxt", "-nt", "-np", "-l", "en",
    ]
    if cfg.whisper_prompt:
        cmd += ["--prompt", cfg.whisper_prompt]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        elapsed = time.perf_counter() - start
        if result.returncode != 0:
            UI.error(
                "Transcription failed: " + (result.stderr.strip().splitlines() or ["?"])[-1],
                hint="Check the model path (WHISPER_MODEL) or run "
                     "`voice_agent.py --check`.",
            )
            return None, elapsed

        txt_path = audio_path + ".txt"
        try:
            text = Path(txt_path).read_text(encoding="utf-8").strip()
        except OSError:
            UI.error("Whisper produced no transcript file")
            return None, elapsed
        finally:
            try:
                os.unlink(txt_path)
            except OSError:
                pass

        # Defensive cleanup for whisper timestamp output.
        cleaned = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and "]" in line:
                line = line.split("]", 1)[1].strip()
            if line:
                cleaned.append(line)
        return " ".join(cleaned).strip(), elapsed
    except subprocess.TimeoutExpired:
        UI.error("Transcription timed out after 60s")
        return None, time.perf_counter() - start
    except FileNotFoundError:
        UI.error(
            f"whisper.cpp not found at {cfg.whisper_cli}",
            hint="Install it with `brew install whisper-cpp` or set WHISPER_CLI.",
        )
        return None, time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001 - report and keep the loop alive
        UI.error(f"Transcription failed: {exc}")
        return None, time.perf_counter() - start


# --------------------------------------------------------------------------- #
# n8n
# --------------------------------------------------------------------------- #

N8N_DOWN_HINT = (
    "n8n runs in Docker. Start Docker Desktop (`open -a Docker`), wait for "
    "the daemon, then check `docker ps` shows the n8n container."
)
N8N_404_HINT = (
    "The webhook is not registered. Make sure the workflow is active, or "
    "redeploy it with ./scripts/deploy_workflow.sh (right after a deploy it "
    "can answer 404 for a second)."
)


def send_to_n8n(cfg: Config, text: str) -> Tuple[Optional[str], float]:
    start = time.perf_counter()
    try:
        response = requests.post(
            cfg.webhook_url,
            json={"chatInput": text, "sessionId": cfg.session_id},
            timeout=cfg.n8n_timeout_s,
        )
        elapsed = time.perf_counter() - start
        if response.status_code != 200:
            UI.error(
                f"n8n answered HTTP {response.status_code}: {response.text[:200]}",
                hint=N8N_404_HINT if response.status_code == 404 else "",
            )
            return None, elapsed
        data = response.json()
        answer = data.get("response")
        if not isinstance(answer, str) or not answer.strip():
            UI.error(f"Unexpected n8n response: {str(data)[:200]}")
            return None, elapsed
        return answer.strip(), elapsed
    except requests.Timeout:
        UI.error(
            f"n8n did not answer within {cfg.n8n_timeout_s:.0f}s",
            hint="General questions go to Ollama; check that it is running "
                 "and the model is pulled.",
        )
        return None, time.perf_counter() - start
    except requests.ConnectionError:
        UI.error(f"Cannot reach n8n at {cfg.webhook_url}", hint=N8N_DOWN_HINT)
        return None, time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001
        UI.error(f"n8n request failed: {exc}")
        return None, time.perf_counter() - start


# --------------------------------------------------------------------------- #
# Text-to-speech
# --------------------------------------------------------------------------- #

class KokoroTTS:
    """Kokoro wrapper that loads the model once and reuses it for every turn."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = None
        self.load_seconds = 0.0
        self.warmup_seconds = 0.0

    def load(self) -> None:
        if self.model is not None:
            return
        start = time.perf_counter()
        from mlx_audio.tts import utils

        self.model = utils.load(self.cfg.kokoro_repo)
        self.load_seconds = time.perf_counter() - start

    def warmup(self) -> None:
        """Create the Kokoro pipeline once so the first real turn is warm."""
        self.load()
        start = time.perf_counter()
        list(
            self.model.generate(
                "Hello.",
                voice=self.cfg.kokoro_voice,
                lang_code=self.cfg.kokoro_lang,
                speed=self.cfg.kokoro_speed,
            )
        )
        self.warmup_seconds = time.perf_counter() - start

    def synthesize(self, text: str, output_path: str) -> Tuple[bool, float, float]:
        """Return (ok, synthesis_seconds, audio_duration_seconds)."""
        start = time.perf_counter()
        try:
            self.load()
            import soundfile as sf

            # The brand name is one word, the phonemiser likes two.
            text = text.replace("DemoShop", "Demo Shop")

            segments = list(
                self.model.generate(
                    text,
                    voice=self.cfg.kokoro_voice,
                    lang_code=self.cfg.kokoro_lang,
                    speed=self.cfg.kokoro_speed,
                )
            )
            if not segments:
                raise RuntimeError("Kokoro returned no audio")
            audio = np.concatenate([np.array(seg.audio) for seg in segments])
            sample_rate = int(segments[0].sample_rate)
            sf.write(output_path, audio, sample_rate)
            return True, time.perf_counter() - start, len(audio) / sample_rate
        except Exception as exc:  # noqa: BLE001
            UI.error(f"Speech synthesis failed: {exc}")
            return False, time.perf_counter() - start, 0.0


def _default_output_index() -> int:
    import sounddevice as sd

    return int(sd.default.device[1])


def list_output_devices() -> list[dict]:
    """The output devices the local audio stack can play to."""
    import sounddevice as sd

    devices = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_output_channels"] > 0:
            devices.append({
                "index": index,
                "name": device["name"],
                "channels": int(device["max_output_channels"]),
                "rate": int(device["default_samplerate"]),
            })
    return devices


def resolve_output_device(name: str) -> Optional[int]:
    """Find an output device by exact name, then by substring."""
    devices = list_output_devices()
    wanted = name.strip().lower()
    for device in devices:
        if device["name"].strip().lower() == wanted:
            return device["index"]
    matches = [d for d in devices if wanted in d["name"].strip().lower()]
    if len(matches) == 1:
        return matches[0]["index"]
    if len(matches) > 1:
        raise RecordingError(
            f"'{name}' matches several devices: "
            + ", ".join(d["name"] for d in matches)
        )
    raise RecordingError(
        f"output device '{name}' not found. Available: "
        + ", ".join(d["name"] for d in devices)
    )


def _play_with_sounddevice(cfg: Config, audio_path: str, device_index: int) -> Tuple[bool, float]:
    """Play the answer on a specific device instead of the system default."""
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    start = time.perf_counter()
    data, rate = sf.read(audio_path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)

    info = sd.query_devices(device_index)
    target_rate = int(info["default_samplerate"]) or int(rate)
    channels = int(info["max_output_channels"])
    if target_rate and rate != target_rate:
        from math import gcd

        from scipy.signal import resample_poly

        divisor = gcd(int(rate), int(target_rate))
        data = resample_poly(data, target_rate // divisor, rate // divisor)
        rate = target_rate
    if channels > 1:
        data = np.repeat(data[:, None], channels, axis=1)

    # An explicit stream starts faster and has less overhead than sd.play().
    with sd.OutputStream(
        device=device_index, channels=channels, samplerate=rate, latency="low"
    ) as stream:
        stream.start()
        stream.write(data)
        stream.stop()
    return True, time.perf_counter() - start


def warmup_output_device(cfg: Config) -> float:
    """
    Open the output device once, so the first answer is not delayed by the
    device activation (the first playback on a cold device can take seconds).
    """
    if not cfg.output_device:
        return 0.0
    start = time.perf_counter()
    try:
        import numpy as np
        import sounddevice as sd

        index = resolve_output_device(cfg.output_device)
        info = sd.query_devices(index)
        rate = int(info["default_samplerate"]) or 48000
        channels = int(info["max_output_channels"])
        data = np.zeros(int(rate * 0.02), dtype="float32")
        if channels > 1:
            data = np.repeat(data[:, None], channels, axis=1)
        with sd.OutputStream(
            device=index, channels=channels, samplerate=rate, latency="low"
        ) as stream:
            stream.start()
            stream.write(data)
            stream.stop()
    except Exception as exc:  # noqa: BLE001
        UI.warn(f"Could not warm up '{cfg.output_device}': {exc}")
        return 0.0
    return time.perf_counter() - start


def play_audio(cfg: Config, audio_path: str) -> Tuple[bool, float]:
    if cfg.output_device:
        start = time.perf_counter()
        try:
            index = resolve_output_device(cfg.output_device)
        except RecordingError as exc:
            UI.error(str(exc), hint="List the devices with --list-output-devices.")
            return False, 0.0
        try:
            return _play_with_sounddevice(cfg, audio_path, index)
        except Exception as exc:  # noqa: BLE001
            UI.error(f"Playback on '{cfg.output_device}' failed: {exc}")
            return False, time.perf_counter() - start
    start = time.perf_counter()
    try:
        result = subprocess.run(
            [cfg.afplay, audio_path],
            capture_output=True,
            timeout=cfg.playback_timeout_s,
        )
        elapsed = time.perf_counter() - start
        if result.returncode != 0:
            UI.error(f"Playback failed: {result.stderr.decode(errors='replace').strip()}")
            return False, elapsed
        return True, elapsed
    except Exception as exc:  # noqa: BLE001
        UI.error(f"Playback failed: {exc}")
        return False, time.perf_counter() - start



# --------------------------------------------------------------------------- #
# Continuous capture (call mode)
# --------------------------------------------------------------------------- #

class MicrophoneFrameSource:
    """
    A long-lived microphone capture.

    The device stays open between turns, so a call does not pay the
    Bluetooth/AVFoundation wake-up time for every sentence.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.frames: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
        self.error: Optional[Exception] = None
        self.level_db = -100.0
        self._thread: Optional[threading.Thread] = None
        self._frame_bytes = int(cfg.sample_rate * FRAME_MS / 1000) * 2 * cfg.channels

    def start(self) -> None:
        cmd = _ffmpeg_input_command(self.cfg) + [
            "-ar", str(self.cfg.sample_rate),
            "-ac", str(self.cfg.channels),
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "pipe:1",
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                frame = self.proc.stdout.read(self._frame_bytes)
                if not frame or len(frame) != self._frame_bytes:
                    break
                self.level_db = _level_db(frame)
                try:
                    self.frames.put_nowait(frame)
                except queue.Full:
                    # The consumer is busy; drop the oldest frame.
                    try:
                        self.frames.get_nowait()
                        self.frames.put_nowait(frame)
                    except (queue.Empty, queue.Full):
                        pass
        except Exception as exc:  # noqa: BLE001
            self.error = exc

    def read(self, timeout: Optional[float] = None) -> Optional[bytes]:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def closed(self) -> bool:
        """True once ffmpeg has exited and every captured frame was read."""
        return (
            self.proc is not None
            and self.proc.poll() is not None
            and self.frames.empty()
        )

    def close_reason(self) -> str:
        try:
            if self.proc is not None and self.proc.stderr is not None:
                text = self.proc.stderr.read().decode(errors="replace").strip()
                if text:
                    return text.splitlines()[-1]
        except (OSError, ValueError):
            pass
        return "ffmpeg stopped delivering audio"

    def drain(self) -> int:
        """Throw away everything that piled up (used while the agent talks)."""
        dropped = 0
        while True:
            try:
                self.frames.get_nowait()
                dropped += 1
            except queue.Empty:
                return dropped

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                if self.proc.stdout is not None:
                    self.proc.stdout.close()
            except OSError:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class WavFrameSource:
    """Plays a WAV file as if it came from the microphone (used by tests)."""

    def __init__(self, cfg: Config, path: str, loop: bool = False, realtime: bool = True):
        self.cfg = cfg
        self.path = path
        self.loop = loop
        self.realtime = realtime
        self._frames: list[bytes] = []
        self._index = 0
        self.level_db = -100.0

    def start(self) -> None:
        with wave.open(self.path, "rb") as wav:
            data = wav.readframes(wav.getnframes())
        frame_bytes = int(self.cfg.sample_rate * FRAME_MS / 1000) * 2 * self.cfg.channels
        self._frames = [
            data[i:i + frame_bytes] for i in range(0, len(data) - frame_bytes + 1, frame_bytes)
        ]
        self._index = 0

    def read(self, timeout: Optional[float] = None) -> Optional[bytes]:
        if self._index >= len(self._frames):
            if not self.loop or not self._frames:
                return None
            self._index = 0
        frame = self._frames[self._index]
        self._index += 1
        self.level_db = _level_db(frame)
        if self.realtime:
            time.sleep(FRAME_MS / 1000.0)
        return frame

    @property
    def closed(self) -> bool:
        return not self.loop and self._index >= len(self._frames)

    def close_reason(self) -> str:
        return f"end of {os.path.basename(self.path)}"

    def drain(self) -> int:
        return 0

    def stop(self) -> None:
        pass


def _play_with_barge_in(
    cfg: Config,
    audio_path: str,
    source,
    stream: VadStream,
    echo_margin_db: float,
    barge_in_ms: int,
) -> Tuple[bool, float, list]:
    """
    Play the answer and watch the microphone.

    Returns (interrupted, playback_seconds, kept_frames). While the answer
    plays the incoming level is compared with the echo level measured at the
    start of the playback; a sustained louder signal is the customer
    interrupting. The frames from the interruption onwards are kept so the
    interrupted sentence is still transcribed.
    """
    start = time.perf_counter()
    proc = subprocess.Popen([cfg.afplay, audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    echo_levels: list[float] = []
    threshold_db = None
    loud_frames = 0
    interrupted = False
    kept: list[bytes] = []
    try:
        while proc.poll() is None:
            frame = source.read(timeout=0.05)
            if frame is None:
                continue
            level = _level_db(frame)
            if len(echo_levels) < 10:
                echo_levels.append(level)
            elif threshold_db is None:
                echo_levels.append(level)
                threshold_db = float(np.median(echo_levels)) + echo_margin_db
            if threshold_db is not None and level > threshold_db:
                loud_frames += 1
                kept.append(frame)
                if loud_frames * FRAME_MS >= barge_in_ms:
                    interrupted = True
                    break
            else:
                loud_frames = max(0, loud_frames - 1)
    finally:
        if interrupted and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
        elif proc.poll() is None:
            try:
                proc.wait(timeout=cfg.playback_timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
    return interrupted, time.perf_counter() - start, kept



# --------------------------------------------------------------------------- #
# Shared turn helpers
# --------------------------------------------------------------------------- #

def prepare_tts(cfg: Config) -> KokoroTTS:
    """Load and warm Kokoro once, and the output device if one is chosen."""
    tts = KokoroTTS(cfg)
    try:
        with UI.working("Loading the voice"):
            tts.warmup()
        UI.check(
            True, "voice",
            f"{cfg.kokoro_voice} ready in {tts.load_seconds + tts.warmup_seconds:.1f}s",
        )
    except Exception as exc:  # noqa: BLE001
        UI.check(
            None, "voice", f"could not pre-load Kokoro: {exc}",
            hint="The first answer will be slower. Run ./scripts/setup.sh if "
                 "the error persists.",
        )
    if cfg.output_device:
        with UI.working(f"Opening {cfg.output_device}"):
            warm = warmup_output_device(cfg)
        UI.check(True, "speaker", f"{cfg.output_device} ({warm:.2f}s)")
    return tts


def _ask(cfg: Config, tts: KokoroTTS, text: str, tts_path: str, speak: bool):
    """
    Send one question to n8n, show the answer and render its audio.

    Returns (response, n8n_s, tts_s, audio_s) or None when the turn failed.
    """
    with UI.working("Thinking"):
        response, n8n_s = send_to_n8n(cfg, text)
    if response is None:
        return None
    if not speak:
        UI.agent(response)
        return response, n8n_s, 0.0, 0.0
    with UI.working("Preparing the voice"):
        ok, tts_s, audio_s = tts.synthesize(response, tts_path)
    UI.agent(response)
    if not ok:
        return None
    return response, n8n_s, tts_s, audio_s


def _goodbye(cfg: Config, tts: KokoroTTS, tts_path: str, play: bool) -> None:
    """The customer is closing the call: answer locally, never via the model."""
    line = farewell_response(cfg.farewell_line or None)
    ok = False
    if play:
        with UI.working("Preparing the voice"):
            ok, _, audio_s = tts.synthesize(line, tts_path)
    UI.agent(line)
    if ok:
        UI.speaking(audio_s, barge_in=False)
        play_audio(cfg, tts_path)
        UI.clear_status()


def _turn_record(cfg: Config, mode: str, turn: int, capture_s: float, stt_s: float,
                 n8n_s: float, tts_s: float, playback_s: float, transcript: str,
                 response: str, **extra) -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "session_id": cfg.session_id,
        "mode": mode,
        "turn": turn,
        "capture_s": capture_s,
        "stt_s": stt_s,
        "n8n_s": n8n_s,
        "tts_s": tts_s,
        "playback_s": playback_s,
        "post_speech_ttfa_s": stt_s + n8n_s + tts_s,
        "ttfa_s": capture_s + stt_s + n8n_s + tts_s,
        "total_s": capture_s + stt_s + n8n_s + tts_s + playback_s,
        "transcript": transcript,
        "response_chars": len(response),
        **extra,
    }


def _next_utterance(source, stream: VadStream) -> Optional[bytes]:
    """Block until the endpointer delivers a turn; None when the input ended."""
    while True:
        frame = source.read(timeout=0.5)
        if frame is None:
            if getattr(source, "error", None) is not None:
                raise source.error
            if getattr(source, "closed", False):
                return None
            continue
        utterance = stream.push(frame)
        if utterance is not None:
            UI.clear_status()
            return utterance
        UI.listening(source.level_db, hearing=stream.started)


# --------------------------------------------------------------------------- #
# Call mode
# --------------------------------------------------------------------------- #

def run_call(cfg: Config, source=None, play: bool = True) -> None:
    """
    Continuous listening, like a customer service call.

    The microphone stays open, the endpointer decides when a sentence is
    finished, and the agent answers without any key press.
    """
    tts = prepare_tts(cfg)
    source = source or MicrophoneFrameSource(cfg)
    stream = VadStream(cfg)

    UI.facts([
        ("session", cfg.session_id),
        ("mic", cfg.audio_device),
        ("turn ends after", f"{cfg.vad_silence_ms} ms of silence"),
        ("barge-in", "on" if cfg.barge_in else "off"),
        ("voice", "on" if play else "off"),
    ])
    UI.debug(f"webhook {cfg.webhook_url} · latency log {cfg.latency_log}")

    try:
        source.start()
    except Exception as exc:  # noqa: BLE001
        UI.error(
            f"Could not open the microphone: {exc}",
            hint="List the microphones with --list-input-devices and pick one "
                 "with --device.",
        )
        return

    UI.hint("Just start talking. Say goodbye or press Ctrl-C to hang up.")
    turn = 0
    ended = "Call ended"
    try:
        while True:
            utterance = _next_utterance(source, stream)
            if utterance is None:
                reason = source.close_reason() if hasattr(source, "close_reason") else ""
                if isinstance(source, MicrophoneFrameSource):
                    UI.error(
                        f"The microphone stopped: {reason}",
                        hint="Was the headset disconnected? Check "
                             "--list-input-devices and start again.",
                    )
                    ended = "Call dropped"
                break

            turn += 1
            metrics = stream.last_metrics
            capture_s = metrics.get("wall_span_s", 0.0)
            with tempfile.TemporaryDirectory(prefix="voice_agent_") as tmp:
                record_path = os.path.join(tmp, "input.wav")
                tts_path = os.path.join(tmp, "response.wav")
                write_wav(record_path, utterance, cfg.sample_rate, cfg.channels)

                with UI.working("Transcribing"):
                    transcript, stt_s = transcribe(cfg, record_path)
                if not transcript:
                    UI.info("(didn't catch that, still listening)")
                    source.drain()
                    continue
                UI.user(transcript)

                if cfg.farewell_enabled and is_farewell(transcript):
                    _goodbye(cfg, tts, tts_path, play)
                    source.drain()
                    break

                answer = _ask(cfg, tts, transcript, tts_path, speak=True)
                if answer is None:
                    source.drain()
                    continue
                response, n8n_s, tts_s, audio_s = answer

                interrupted = False
                playback_s = 0.0
                if play:
                    UI.speaking(audio_s, cfg.barge_in)
                    if cfg.barge_in:
                        interrupted, playback_s, kept = _play_with_barge_in(
                            cfg, tts_path, source, stream,
                            cfg.barge_in_db, cfg.barge_in_ms,
                        )
                        # The frames after the interruption belong to the
                        # customer, so they are fed to the endpointer.
                        for kept_frame in kept:
                            stream.push(kept_frame)
                    else:
                        # Half duplex: the microphone is ignored while the
                        # agent talks, so the answer cannot trigger itself.
                        source.drain()
                        _, playback_s = play_audio(cfg, tts_path)
                        # Drop the echo of our own answer.
                        source.drain()
                    UI.clear_status()
                else:
                    source.drain()

            rows = _turn_record(
                cfg, "call", turn, capture_s, stt_s, n8n_s, tts_s, playback_s,
                transcript, response, interrupted=interrupted, vad=metrics,
            )
            UI.timing(rows)
            log_latency(cfg, rows)
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
        UI.clear_status()
        UI.summary(ended)


# --------------------------------------------------------------------------- #
# Latency log
# --------------------------------------------------------------------------- #

def log_latency(cfg: Config, record: dict) -> None:
    try:
        cfg.latency_log.parent.mkdir(parents=True, exist_ok=True)
        with cfg.latency_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        UI.warn(f"Could not write the latency log: {exc}")


# --------------------------------------------------------------------------- #
# Arm / push-to-talk mode
# --------------------------------------------------------------------------- #

def run(cfg: Config, mode: str) -> None:
    tts = prepare_tts(cfg)
    vad_available = mode == "auto"
    UI.facts([
        ("session", cfg.session_id),
        ("mic", cfg.audio_device),
        ("turn", f"ends after {cfg.vad_silence_ms} ms of silence"
                 if vad_available else f"fixed {cfg.ptt_duration:.1f}s"),
    ])
    UI.debug(f"webhook {cfg.webhook_url} · latency log {cfg.latency_log}")

    turn = 0
    ended = "Session ended"
    while True:
        try:
            keys = "Enter to talk" + (" · p fixed-length" if vad_available else "") + " · q quit"
            print()
            choice = input(f"  {UI.s.dim(keys)} ").strip().lower()
            if choice in ("q", "quit", "exit"):
                break

            turn_mode = "ptt" if choice == "p" else ("vad" if vad_available else "ptt")

            with tempfile.TemporaryDirectory(prefix="voice_agent_") as tmp:
                record_path = os.path.join(tmp, "input.wav")
                tts_path = os.path.join(tmp, "response.wav")

                capture_start = time.perf_counter()
                vad_metrics: dict = {}
                if turn_mode == "vad":
                    try:
                        with UI.working("Listening, speak now"):
                            _, capture_s, vad_metrics = record_vad(cfg, record_path)
                        UI.debug(
                            f"vad: open {vad_metrics.get('device_open_s', 0):.2f}s · "
                            f"wait {vad_metrics.get('wait_s', 0):.2f}s · "
                            f"speech {vad_metrics.get('speech_s', 0):.2f}s · "
                            f"stop {vad_metrics.get('stop_reason', '?')}"
                        )
                    except NoSpeechDetected as exc:
                        UI.info(f"({exc}, nothing sent)")
                        continue
                    except Exception as exc:  # noqa: BLE001
                        UI.warn(
                            f"Voice detection unavailable: {exc}",
                            hint=f"Falling back to {cfg.ptt_duration:.1f}s fixed-length recording.",
                        )
                        vad_available = False
                        turn_mode = "ptt"
                        try:
                            with UI.working(f"Recording {cfg.ptt_duration:.0f}s, speak now"):
                                record_fixed(cfg, record_path, cfg.ptt_duration)
                        except RecordingError as fallback_exc:
                            UI.error(f"Recording failed: {fallback_exc}")
                            continue
                        capture_s = time.perf_counter() - capture_start
                else:
                    with UI.working(f"Recording {cfg.ptt_duration:.0f}s, speak now"):
                        record_fixed(cfg, record_path, cfg.ptt_duration)
                    capture_s = time.perf_counter() - capture_start

                with UI.working("Transcribing"):
                    text, stt_s = transcribe(cfg, record_path)
                if not text:
                    if text is not None:
                        UI.info("(no speech detected)")
                    continue
                UI.user(text)

                if cfg.farewell_enabled and is_farewell(text):
                    _goodbye(cfg, tts, tts_path, play=True)
                    break

                answer = _ask(cfg, tts, text, tts_path, speak=True)
                if answer is None:
                    continue
                response, n8n_s, tts_s, audio_s = answer

                UI.speaking(audio_s, barge_in=False)
                ok, playback_s = play_audio(cfg, tts_path)
                UI.clear_status()

                turn += 1
                rows = _turn_record(
                    cfg, turn_mode, turn, capture_s, stt_s, n8n_s, tts_s, playback_s,
                    text, response, playback_ok=ok, audio_duration_s=audio_s,
                    vad=vad_metrics,
                )
                UI.timing(rows)
                log_latency(cfg, rows)

        except (KeyboardInterrupt, EOFError):
            break
        except RecordingError as exc:
            UI.error(
                f"Recording failed: {exc}",
                hint="List the microphones with --list-input-devices.",
            )
        except Exception as exc:  # noqa: BLE001 - keep the interactive loop alive
            UI.error(str(exc))
    UI.clear_status()
    UI.summary(ended)


# --------------------------------------------------------------------------- #
# Text mode
# --------------------------------------------------------------------------- #

def run_text(cfg: Config, play: bool = True) -> None:
    """Type the questions; the answers are shown and (optionally) spoken."""
    tts = prepare_tts(cfg) if play else KokoroTTS(cfg)
    UI.facts([("session", cfg.session_id), ("voice", "on" if play else "off")])
    UI.debug(f"webhook {cfg.webhook_url} · latency log {cfg.latency_log}")
    UI.hint("Type a question and press Enter. Ctrl-D or 'bye' to leave.")

    turn = 0
    while True:
        try:
            print()
            text = input(f"  {UI.s.cyan('You')}    ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not text:
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="voice_agent_") as tmp:
                tts_path = os.path.join(tmp, "response.wav")
                if cfg.farewell_enabled and is_farewell(text):
                    _goodbye(cfg, tts, tts_path, play)
                    break
                answer = _ask(cfg, tts, text, tts_path, speak=play)
                if answer is None:
                    continue
                response, n8n_s, tts_s, audio_s = answer
                playback_s = 0.0
                if play:
                    UI.speaking(audio_s, barge_in=False)
                    _, playback_s = play_audio(cfg, tts_path)
                    UI.clear_status()
            turn += 1
            rows = _turn_record(
                cfg, "text", turn, 0.0, 0.0, n8n_s, tts_s, playback_s, text, response,
            )
            UI.timing(rows)
            log_latency(cfg, rows)
        except KeyboardInterrupt:
            UI.clear_status()
            UI.info("(interrupted)")
    UI.clear_status()
    UI.summary("Session ended")


# --------------------------------------------------------------------------- #
# Startup checks and devices
# --------------------------------------------------------------------------- #

def list_input_devices(cfg: Config) -> Optional[list[tuple[int, str]]]:
    """The AVFoundation microphones as (index, name); None if ffmpeg failed."""
    try:
        result = subprocess.run(
            [cfg.ffmpeg, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_avfoundation_devices(result.stderr)


def parse_avfoundation_devices(listing: str) -> list[tuple[int, str]]:
    """The audio section of `ffmpeg -f avfoundation -list_devices true`."""
    devices: list[tuple[int, str]] = []
    in_audio = False
    for line in listing.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if in_audio and "] [" in line:
            tail = line.split("] [", 1)[1]
            index, _, name = tail.partition("] ")
            if index.isdigit():
                devices.append((int(index), name.strip()))
    return devices


NO_MIC_HINT = (
    "Connect the headset, and allow the terminal app to use the microphone "
    "in System Settings > Privacy & Security > Microphone."
)


def _n8n_reachable(url: str, timeout: float = 1.0) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def preflight(cfg: Config, mode: str) -> Tuple[bool, bool]:
    """
    Print the startup checklist.

    Returns (can_run, all_ok). can_run is False when something the chosen
    mode cannot work without is missing; all_ok also counts warnings.
    """
    can_run = True
    all_ok = True
    uses_mic = mode != "text"

    if uses_mic:
        ffmpeg = shutil.which(cfg.ffmpeg)
        UI.check(bool(ffmpeg), "ffmpeg", ffmpeg or "not found",
                 hint="Install it with `brew install ffmpeg`.")
        can_run &= bool(ffmpeg)

        whisper = (
            cfg.whisper_cli if os.access(cfg.whisper_cli, os.X_OK)
            else shutil.which(cfg.whisper_cli)
        )
        UI.check(bool(whisper), "whisper.cpp", whisper or f"not found at {cfg.whisper_cli}",
                 hint="Install it with `brew install whisper-cpp` or set WHISPER_CLI.")
        can_run &= bool(whisper)

        model = Path(cfg.whisper_model)
        UI.check(model.is_file(), "model",
                 model.name if model.is_file() else f"missing: {model}",
                 hint="./scripts/setup.sh downloads it (141 MB).")
        can_run &= model.is_file()

        if ffmpeg and cfg.audio_input_format == "avfoundation":
            devices = list_input_devices(cfg)
            wanted = cfg.audio_device.lstrip(":")
            if devices == []:
                UI.check(
                    None, "microphone", "ffmpeg sees no audio input at all",
                    hint=NO_MIC_HINT,
                )
                all_ok = False
            elif devices is not None and wanted.isdigit():
                names = dict(devices)
                found = int(wanted) in names
                UI.check(
                    found, "microphone",
                    f"{cfg.audio_device} {names[int(wanted)]}" if found
                    else f"no audio device {cfg.audio_device}",
                    hint="Available: " + ", ".join(f":{i} {n}" for i, n in devices)
                    + ". Choose one with --device.",
                )
                can_run &= found

    n8n_ok = _n8n_reachable(cfg.webhook_url)
    netloc = urlparse(cfg.webhook_url).netloc or cfg.webhook_url
    UI.check(True if n8n_ok else None, "n8n",
             netloc if n8n_ok else f"nothing listening on {netloc}",
             hint=N8N_DOWN_HINT)
    all_ok &= n8n_ok

    if cfg.output_device:
        try:
            resolve_output_device(cfg.output_device)
        except Exception as exc:  # noqa: BLE001
            UI.check(False, "speaker", str(exc),
                     hint="List the devices with --list-output-devices.")
            can_run = False

    return can_run, all_ok and can_run


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

MODE_LABELS = {
    "call": "call mode",
    "auto": "press Enter to talk",
    "ptt": "fixed-length recording",
    "text": "text mode",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fully local DemoShop voice agent",
    )
    parser.add_argument(
        "-m", "--mode", choices=["call", "arm", "auto", "ptt", "text"],
        default=_env("VOICE_MODE", "call"),
        help="call = always listening, arm = press Enter to talk, "
             "ptt = fixed length, text = type the questions",
    )
    parser.add_argument("--text", action="store_true", help="shortcut for --mode text")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show the full latency and VAD breakdown")
    parser.add_argument("--check", action="store_true",
                        help="only run the startup checks and exit (non-zero on a problem)")
    parser.add_argument("--no-color", action="store_true", help="plain output")
    parser.add_argument(
        "--barge-in", action="store_true",
        help="allow the customer to interrupt the answer (call mode)",
    )
    parser.add_argument(
        "--no-play", action="store_true",
        help="do not speak the answers (useful while testing)",
    )
    parser.add_argument(
        "--no-farewell", action="store_true",
        help="do not end the call on a goodbye phrase",
    )
    parser.add_argument(
        "--farewell-line", type=str, default=None,
        help="what the agent says when the customer says goodbye",
    )
    parser.add_argument(
        "--output-device", type=str, default=None,
        help="play the answers on this output device (default: the macOS default)",
    )
    parser.add_argument(
        "--list-output-devices", action="store_true",
        help="list the available output devices and exit",
    )
    parser.add_argument(
        "--list-input-devices", action="store_true",
        help="list the available microphones and exit",
    )
    parser.add_argument(
        "-d", "--duration", type=float, default=None,
        help="fixed push-to-talk duration in seconds (default 5)",
    )
    parser.add_argument("--session", type=str, default=None, help="stable session ID")
    parser.add_argument("--device", type=str, default=None, help="AVFoundation audio device, e.g. :0")
    parser.add_argument("--webhook", type=str, default=None, help="n8n voice webhook URL")
    parser.add_argument("--silence-ms", type=int, default=None, help="VAD end-of-speech silence (ms)")
    parser.add_argument("--max-seconds", type=float, default=None, help="VAD maximum recording time")
    parser.add_argument("--noise-margin-db", type=float, default=None,
                        help="how far above the background a frame must be to count as speech")
    parser.add_argument("--snr-gap-db", type=float, default=None,
                        help="how far below the speech level a frame must be to count as silence")
    return parser.parse_args()


def main() -> None:
    global UI
    args = parse_args()
    UI = Console(color=False if args.no_color else None, verbose=args.verbose)

    cfg = Config()
    if args.duration is not None:
        cfg.ptt_duration = args.duration
    if args.session:
        cfg.session_id = args.session
    if args.device:
        cfg.audio_device = args.device
    if args.webhook:
        cfg.webhook_url = args.webhook
    if args.silence_ms is not None:
        cfg.vad_silence_ms = args.silence_ms
    if args.max_seconds is not None:
        cfg.vad_max_seconds = args.max_seconds
    if args.noise_margin_db is not None:
        cfg.vad_noise_margin_db = args.noise_margin_db
    if args.snr_gap_db is not None:
        cfg.vad_snr_gap_db = args.snr_gap_db
    if args.barge_in:
        cfg.barge_in = True
    if args.no_farewell:
        cfg.farewell_enabled = False
    if args.farewell_line is not None:
        cfg.farewell_line = args.farewell_line
    if args.output_device is not None:
        cfg.output_device = args.output_device

    if args.list_output_devices:
        print("Output devices:")
        default = _default_output_index()
        for device in list_output_devices():
            marker = " (default)" if device["index"] == default else ""
            print(f"  [{device['index']}] {device['name']}  {device['rate']} Hz  {device['channels']} ch{marker}")
        return

    if args.list_input_devices:
        devices = list_input_devices(cfg)
        if devices is None:
            print("Could not ask ffmpeg for the devices (is ffmpeg installed?)", file=sys.stderr)
            sys.exit(1)
        if not devices:
            print("No microphone found. " + NO_MIC_HINT)
            sys.exit(1)
        print("Microphones (use with --device):")
        for index, name in devices:
            marker = "  <- current" if cfg.audio_device == f":{index}" else ""
            print(f"  :{index}  {name}{marker}")
        return

    mode = "text" if args.text else ("auto" if args.mode == "arm" else args.mode)

    UI.title(f"DemoShop Voice Agent · {MODE_LABELS[mode]}")
    can_run, all_ok = preflight(cfg, mode)
    if args.check:
        UI.hint("All checks passed." if all_ok else "Some checks need attention.")
        sys.exit(0 if all_ok else 1)
    if not can_run:
        UI.error("Cannot start until the problems above are fixed.")
        sys.exit(1)

    if mode == "call":
        run_call(cfg, play=not args.no_play)
    elif mode == "text":
        run_text(cfg, play=not args.no_play)
    else:
        run(cfg, mode)


if __name__ == "__main__":
    main()
