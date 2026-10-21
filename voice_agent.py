#!/usr/bin/env python3
"""
Fully local voice customer-support client.

Pipeline:
    microphone -> ffmpeg/avfoundation -> (WebRTC VAD) -> whisper.cpp
    -> n8n voice-agent webhook -> Kokoro TTS (loaded once) -> afplay

Modes:
    auto  Voice activity detection stops the recording automatically (default).
    ptt   Fixed-duration push-to-talk, used as a fallback.

Configuration is via CLI flags or environment variables. No secrets are used
or stored by this client.
"""

import argparse
import collections
import json
import math
import os
import queue
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

import numpy as np
import requests

from farewell import farewell_response, is_farewell
from vad_stream import (
    FRAME_MS,
    NoSpeechDetected,
    VadStream,
    _is_stationary,
    _level_db,
    _percentile,
)

BASE_DIR = Path(__file__).resolve().parent


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

FRAME_MS = 30


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
            print(f"  [ERROR] Transcription failed: {result.stderr.strip()}", file=sys.stderr)
            return None, elapsed

        txt_path = audio_path + ".txt"
        try:
            text = Path(txt_path).read_text(encoding="utf-8").strip()
        except OSError:
            print("  [ERROR] Whisper produced no transcript file", file=sys.stderr)
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
        return None, time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001 - report and keep the loop alive
        print(f"  [ERROR] Transcription exception: {exc}", file=sys.stderr)
        return None, time.perf_counter() - start


# --------------------------------------------------------------------------- #
# n8n
# --------------------------------------------------------------------------- #

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
            print(f"  [ERROR] n8n HTTP {response.status_code}: {response.text[:300]}", file=sys.stderr)
            return None, elapsed
        data = response.json()
        answer = data.get("response")
        if not isinstance(answer, str) or not answer.strip():
            print(f"  [ERROR] Unexpected n8n response: {data}", file=sys.stderr)
            return None, elapsed
        return answer.strip(), elapsed
    except requests.Timeout:
        print("  [ERROR] n8n request timed out", file=sys.stderr)
        return None, time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERROR] n8n request exception: {exc}", file=sys.stderr)
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
            print(f"  [ERROR] TTS generation failed: {exc}", file=sys.stderr)
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
        print(f"  [WARN] Could not warm up '{cfg.output_device}': {exc}", file=sys.stderr)
        return 0.0
    return time.perf_counter() - start


def play_audio(cfg: Config, audio_path: str) -> Tuple[bool, float]:
    if cfg.output_device:
        start = time.perf_counter()
        try:
            index = resolve_output_device(cfg.output_device)
        except RecordingError as exc:
            print(f"  [ERROR] {exc}", file=sys.stderr)
            return False, 0.0
        try:
            return _play_with_sounddevice(cfg, audio_path, index)
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERROR] Playback on '{cfg.output_device}' failed: {exc}", file=sys.stderr)
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
            print(f"  [ERROR] Playback failed: {result.stderr.decode(errors='replace')}", file=sys.stderr)
            return False, elapsed
        return True, elapsed
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERROR] Playback exception: {exc}", file=sys.stderr)
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


def run_call(cfg: Config, source=None, play: bool = True) -> None:
    """
    Continuous listening, like a customer service call.

    The microphone stays open, the endpointer decides when a sentence is
    finished, and the agent answers without any key press.
    """
    tts = KokoroTTS(cfg)
    print("  [INFO] Loading and warming Kokoro TTS once...")
    try:
        tts.warmup()
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] Could not pre-load Kokoro: {exc}", file=sys.stderr)
    if cfg.output_device:
        warm = warmup_output_device(cfg)
        print(f"  [INFO] Output device '{cfg.output_device}' ready ({warm:.2f}s)")

    source = source or MicrophoneFrameSource(cfg)
    stream = VadStream(cfg)

    print("=" * 68)
    print("  DemoShop Voice Agent - continuous call mode")
    print("=" * 68)
    print(f"  Session ID : {cfg.session_id}")
    print(f"  Webhook    : {cfg.webhook_url}")
    print(f"  Mic device : {cfg.audio_device} (AVFoundation, stays open)")
    print(
        f"  Turn end   : {cfg.vad_silence_ms} ms of silence "
        f"(max {cfg.vad_max_seconds:.0f}s per turn)"
    )
    print(f"  Barge-in   : {'on' if cfg.barge_in else 'off (half duplex)'}")
    print(f"  Latency log: {cfg.latency_log}")
    print("-" * 68)

    try:
        source.start()
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERROR] Could not open the microphone: {exc}", file=sys.stderr)
        return

    print("  [READY] Listening. Just start talking. Ctrl-C to hang up.")
    turn = 0
    try:
        while True:
            utterance = None
            while utterance is None:
                frame = source.read(timeout=0.5)
                if frame is None:
                    if getattr(source, "error", None) is not None:
                        raise source.error
                    continue
                utterance = stream.push(frame)
            if utterance is None:
                break

            turn += 1
            metrics = stream.last_metrics
            with tempfile.TemporaryDirectory(prefix="voice_agent_") as tmp:
                record_path = os.path.join(tmp, "input.wav")
                tts_path = os.path.join(tmp, "response.wav")
                write_wav(record_path, utterance, cfg.sample_rate, cfg.channels)

                transcript, stt_s = transcribe(cfg, record_path)
                if not transcript:
                    print("  [STT] Nothing understood, still listening.")
                    source.drain()
                    continue
                print(f'  [STT] "{transcript}" ({stt_s:.2f}s)')

                if cfg.farewell_enabled and is_farewell(transcript):
                    # The customer is closing the call: answer locally and
                    # hang up instead of waking the language model.
                    line = farewell_response(cfg.farewell_line or None)
                    print(f'  [BYE] "{transcript}" -> closing the call')
                    if play:
                        ok, tts_s, audio_s = tts.synthesize(line, tts_path)
                        if ok:
                            print(f"  [PLAY] {line}")
                            play_audio(cfg, tts_path)
                    else:
                        tts_s = 0.0
                    print_latency({
                        "capture_s": metrics.get("wall_span_s", 0.0),
                        "stt_s": stt_s,
                        "n8n_s": 0.0,
                        "tts_s": tts_s,
                        "playback_s": 0.0,
                        "post_speech_ttfa_s": stt_s + tts_s,
                        "ttfa_s": metrics.get("wall_span_s", 0.0) + stt_s + tts_s,
                        "total_s": metrics.get("wall_span_s", 0.0) + stt_s + tts_s,
                    })
                    source.drain()
                    print("  [CALL ENDED] Goodbye!")
                    return

                response, n8n_s = send_to_n8n(cfg, transcript)
                if response is None:
                    source.drain()
                    continue
                preview = response if len(response) <= 160 else response[:157] + "..."
                print(f'  [N8N] "{preview}" ({n8n_s:.2f}s)')

                ok, tts_s, audio_s = tts.synthesize(response, tts_path)
                if not ok:
                    source.drain()
                    continue

                if play:
                    print(f"  [PLAY] {audio_s:.2f}s of audio")
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
                        interrupted = False
                        # Drop the echo of our own answer.
                        source.drain()
                else:
                    source.drain()
                    playback_s = 0.0
                    interrupted = False

            total_s = metrics.get("wall_span_s", 0.0) + stt_s + n8n_s + tts_s + playback_s
            rows = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "session_id": cfg.session_id,
                "mode": "call",
                "turn": turn,
                "capture_s": metrics.get("wall_span_s", 0.0),
                "stt_s": stt_s,
                "n8n_s": n8n_s,
                "tts_s": tts_s,
                "playback_s": playback_s,
                "interrupted": interrupted,
                "post_speech_ttfa_s": stt_s + n8n_s + tts_s,
                "ttfa_s": metrics.get("wall_span_s", 0.0) + stt_s + n8n_s + tts_s,
                "total_s": total_s,
                "transcript": transcript,
                "response_chars": len(response),
                "vad": metrics,
            }
            print_latency(rows)
            log_latency(cfg, rows)
            if interrupted:
                print("  [BARGE-IN] The customer interrupted, listening again.")
    except KeyboardInterrupt:
        print("\n  Bye.")
    finally:
        source.stop()

# --------------------------------------------------------------------------- #
# Latency log
# --------------------------------------------------------------------------- #

def log_latency(cfg: Config, record: dict) -> None:
    try:
        cfg.latency_log.parent.mkdir(parents=True, exist_ok=True)
        with cfg.latency_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"  [WARN] Could not write latency log: {exc}", file=sys.stderr)


def print_latency(rows: dict) -> None:
    print(
        "  [LATENCY] "
        f"capture:{rows['capture_s']:.2f}s "
        f"stt:{rows['stt_s']:.2f}s "
        f"n8n:{rows['n8n_s']:.2f}s "
        f"tts:{rows['tts_s']:.2f}s "
        f"play:{rows['playback_s']:.2f}s | "
        f"first audio after speech:{rows['post_speech_ttfa_s']:.2f}s "
        f"first audio from arm:{rows['ttfa_s']:.2f}s "
        f"total:{rows['total_s']:.2f}s"
    )


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def print_banner(cfg: Config, mode: str, tts: KokoroTTS) -> None:
    print("=" * 68)
    print("  DemoShop Voice Agent - fully local")
    print("=" * 68)
    print(f"  Session ID : {cfg.session_id}")
    print(f"  Webhook    : {cfg.webhook_url}")
    print(f"  Mic device : {cfg.audio_device} (AVFoundation)")
    print(f"  Mode       : {mode}")
    if mode == "auto":
        print(
            f"  VAD        : silence {cfg.vad_silence_ms} ms, "
            f"max {cfg.vad_max_seconds:.0f}s, min speech {cfg.vad_min_speech_ms} ms, "
            f"noise margin {cfg.vad_noise_margin_db:.0f} dB, snr gap {cfg.vad_snr_gap_db:.0f} dB"
        )
    else:
        print(f"  Record     : fixed {cfg.ptt_duration:.1f}s")
    print(
        f"  Kokoro     : {cfg.kokoro_repo} / {cfg.kokoro_voice} "
        f"(loaded once, {tts.load_seconds:.2f}s load + {tts.warmup_seconds:.2f}s warm-up, speed {cfg.kokoro_speed})"
    )
    print(f"  Latency log: {cfg.latency_log}")
    print("-" * 68)


def run(cfg: Config, mode: str) -> None:
    tts = KokoroTTS(cfg)
    print("  [INFO] Loading and warming Kokoro TTS once...")
    try:
        tts.warmup()
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] Could not pre-load Kokoro: {exc}", file=sys.stderr)
    if cfg.output_device:
        warm = warmup_output_device(cfg)
        print(f"  [INFO] Output device '{cfg.output_device}' ready ({warm:.2f}s)")

    print_banner(cfg, mode, tts)
    vad_available = mode == "auto"

    while True:
        try:
            prompt = "\n[Enter to speak" + (", 'p' push-to-talk" if vad_available else "") + ", q to quit] > "
            choice = input(prompt).strip().lower()
            if choice == "q":
                print("\nGoodbye!")
                return

            turn_mode = "ptt" if choice == "p" else ("vad" if vad_available else "ptt")

            with tempfile.TemporaryDirectory(prefix="voice_agent_") as tmp:
                record_path = os.path.join(tmp, "input.wav")
                tts_path = os.path.join(tmp, "response.wav")

                capture_start = time.perf_counter()
                vad_metrics: dict = {}
                if turn_mode == "vad":
                    print("  [REC] Listening... speak now (auto-stops on silence)")
                    try:
                        _, capture_s, vad_metrics = record_vad(cfg, record_path)
                        print(
                            "  [REC] VAD "
                            f"open {vad_metrics.get('device_open_s', 0):.2f}s | "
                            f"wait {vad_metrics.get('wait_s', 0):.2f}s | "
                            f"speech {vad_metrics.get('speech_s', 0):.2f}s | "
                            f"silence {vad_metrics.get('end_of_speech_delay_ms', 0) / 1000:.2f}s | "
                            f"stop {vad_metrics.get('stop_reason', '?')} | "
                            f"capture {capture_s:.2f}s"
                        )
                    except NoSpeechDetected as exc:
                        print(f"  [REC] {exc}. Nothing sent.")
                        continue
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [WARN] VAD capture unavailable: {exc}")
                        print(f"  [REC] Falling back to {cfg.ptt_duration:.1f}s push-to-talk")
                        vad_available = False
                        turn_mode = "ptt"
                        try:
                            _, _ = record_fixed(cfg, record_path, cfg.ptt_duration)
                        except RecordingError as fallback_exc:
                            print(f"  [ERROR] Push-to-talk fallback failed: {fallback_exc}", file=sys.stderr)
                            continue
                        capture_s = time.perf_counter() - capture_start
                else:
                    print(f"  [REC] Recording for {cfg.ptt_duration:.1f}s... speak now")
                    _, _ = record_fixed(cfg, record_path, cfg.ptt_duration)
                    capture_s = time.perf_counter() - capture_start

                text, stt_s = transcribe(cfg, record_path)
                if text is None:
                    continue
                if not text:
                    print("  [STT] No speech detected")
                    continue
                print(f'  [STT] "{text}" ({stt_s:.2f}s)')

                if cfg.farewell_enabled and is_farewell(text):
                    line = farewell_response(cfg.farewell_line or None)
                    print(f'  [BYE] "{text}" -> closing the call')
                    ok, tts_s, _ = tts.synthesize(line, tts_path)
                    if ok:
                        print(f"  [PLAY] {line}")
                        play_audio(cfg, tts_path)
                    print("  [CALL ENDED] Goodbye!")
                    return

                response, n8n_s = send_to_n8n(cfg, text)
                if response is None:
                    continue
                preview = response if len(response) <= 160 else response[:157] + "..."
                print(f'  [N8N] "{preview}" ({n8n_s:.2f}s)')

                ok, tts_s, audio_s = tts.synthesize(response, tts_path)
                if not ok:
                    continue
                print(f"  [TTS] {audio_s:.2f}s of audio in {tts_s:.2f}s (warm model)")

                print("  [PLAY] Playing response...")
                ok, playback_s = play_audio(cfg, tts_path)

                total_s = capture_s + stt_s + n8n_s + tts_s + playback_s
                rows = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "session_id": cfg.session_id,
                    "mode": turn_mode,
                    "capture_s": capture_s,
                    "stt_s": stt_s,
                    "n8n_s": n8n_s,
                    "tts_s": tts_s,
                    "playback_s": playback_s,
                    "playback_ok": ok,
                    "audio_duration_s": audio_s,
                    "post_speech_ttfa_s": stt_s + n8n_s + tts_s,
                    "ttfa_s": capture_s + stt_s + n8n_s + tts_s,
                    "total_s": total_s,
                    "transcript": text,
                    "response_chars": len(response),
                    "vad": vad_metrics,
                }
                print_latency(rows)
                log_latency(cfg, rows)
                if not ok:
                    continue

        except KeyboardInterrupt:
            print("\n\nInterrupted. Goodbye!")
            return
        except EOFError:
            print("\nGoodbye!")
            return
        except RecordingError as exc:
            print(f"  [ERROR] Recording failed: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - keep the interactive loop alive
            print(f"  [ERROR] {exc}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fully local DemoShop voice agent")
    parser.add_argument(
        "-m", "--mode", choices=["call", "arm", "auto", "ptt"],
        default=_env("VOICE_MODE", "call"),
        help="call = always listening (default), arm = press Enter to talk, ptt = fixed length",
    )
    parser.add_argument(
        "--barge-in", action="store_true",
        help="allow the customer to interrupt the answer (call mode)",
    )
    parser.add_argument(
        "--no-play", action="store_true",
        help="do not speak the answers (call mode, useful while testing)",
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
        "-d", "--duration", type=float, default=None,
        help="Fixed push-to-talk duration in seconds (default 5)",
    )
    parser.add_argument("--session", type=str, default=None, help="Stable session ID")
    parser.add_argument("--device", type=str, default=None, help="AVFoundation audio device, e.g. :0")
    parser.add_argument("--webhook", type=str, default=None, help="n8n voice webhook URL")
    parser.add_argument("--silence-ms", type=int, default=None, help="VAD end-of-speech silence (ms)")
    parser.add_argument("--max-seconds", type=float, default=None, help="VAD maximum recording time")
    parser.add_argument("--noise-margin-db", type=float, default=None,
                        help="How far above the background a frame must be to count as speech")
    parser.add_argument("--snr-gap-db", type=float, default=None,
                        help="How far below the speech level a frame must be to count as silence")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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
        for device in list_output_devices():
            marker = " (default)" if device["index"] == _default_output_index() else ""
            print(f"  [{device['index']}] {device['name']}  {device['rate']} Hz  {device['channels']} ch{marker}")
        return

    if args.mode == "call":
        run_call(cfg, play=not args.no_play)
    else:
        run(cfg, "auto" if args.mode == "arm" else args.mode)


if __name__ == "__main__":
    main()
