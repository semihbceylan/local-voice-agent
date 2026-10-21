"""
Resumable voice activity detection.

The state machine lives here so that both the single recording path
(segment_speech) and the continuous call mode (VadStream) share exactly the
same logic. voice_agent.py imports from this module.
"""

import collections
import math
import time
from typing import Optional, Tuple

import numpy as np

FRAME_MS = 30


class NoSpeechDetected(RuntimeError):
    pass


def _level_db(frame: bytes) -> float:
    """RMS level of one PCM frame in dBFS (floored at -100 dBFS)."""
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(samples))))
    if rms <= 1e-7:
        return -100.0
    return max(-100.0, 20.0 * math.log10(rms))


def _percentile(values, pct: float) -> float:
    if not len(values):
        return -100.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), pct))


def _is_stationary(history, cfg) -> bool:
    """True when the smoothed level hardly moves over the last ~350 ms."""
    if len(history) < 8:
        return False
    return (max(history) - min(history)) <= cfg.vad_stuck_flat_db


class VadStream:
    """
    Resumable voice activity detection for a continuous microphone stream.

    The same logic that segment_speech() used for a single recording, but
    kept as a state machine so one long-lived capture can produce utterance
    after utterance:

        stream = VadStream(cfg)
        for frame in frames:
            utterance = stream.push(frame)
            if utterance is not None:
                ... handle the utterance ...

    The learned noise floor and the VAD state survive between turns, because
    the room does not change between two sentences.
    """

    def __init__(self, cfg, vad=None, clock=time.perf_counter):
        self.cfg = cfg
        self.clock = clock
        if vad is None:
            import webrtcvad  # local dependency: webrtcvad-wheels

            vad = webrtcvad.Vad(cfg.vad_aggressiveness)
        self.vad = vad

        self.frame_bytes = int(cfg.sample_rate * FRAME_MS / 1000) * 2 * cfg.channels
        self.silence_frames = max(1, cfg.vad_silence_ms // FRAME_MS)
        self.pre_roll_frames = max(1, cfg.vad_preroll_ms // FRAME_MS)
        self.keep_tail_frames = max(0, cfg.vad_keep_trailing_silence_ms // FRAME_MS)
        self.reset_interval = max(cfg.vad_reset_frames, self.silence_frames + 3)

        # Room level state, kept across turns.
        self.level_history = collections.deque(maxlen=100)
        self.signal_levels = collections.deque(maxlen=200)
        self.smooth_history = collections.deque(maxlen=12)
        self.min_level_db = None
        self.bg_db = None
        self.background_known = False
        self.speech_db = None
        self.vad_unreliable = False
        self.frame_index = 0

        # Turn state.
        self.pre_roll = collections.deque(maxlen=self.pre_roll_frames)
        self.recorded: list[bytes] = []
        self.started = False
        self.onset_index = None
        self.last_signal_index = None
        self.last_voiced_index = None
        self.voiced_run = 0
        self.voiced_run_levels: list[float] = []
        self.quiet_run_frames = 0
        self.signal_frames = 0
        self.vad_voiced_frames = 0
        self.quiet_frames = 0
        self.speech_elapsed_ms = 0
        self.wait_ms = 0
        self.background_floor_db = -100.0
        self.background_samples = 0
        self.vad_speech_run = 0
        self.vad_conflict_frames = 0
        self.vad_conflict_max = 0
        self.smooth_db = None
        self.prev_smooth_db = None
        self.first_frame_at = None
        self.onset_at = None
        self.last_voiced_at = None
        self.stop_reason = "eof"
        self.stop_index = 0
        self.stop_at = None
        self.last_metrics: dict = {}

    # -- turn lifecycle ----------------------------------------------------

    def reset_turn(self) -> None:
        """Prepare for the next utterance, keeping the room level."""
        self.pre_roll.clear()
        self.recorded = []
        self.started = False
        self.onset_index = None
        self.last_signal_index = None
        self.last_voiced_index = None
        self.voiced_run = 0
        self.voiced_run_levels = []
        self.quiet_run_frames = 0
        self.signal_frames = 0
        self.vad_voiced_frames = 0
        self.quiet_frames = 0
        self.speech_elapsed_ms = 0
        self.wait_ms = 0
        self.vad_speech_run = 0
        self.vad_conflict_frames = 0
        self.smooth_db = None
        self.prev_smooth_db = None
        self.first_frame_at = None
        self.onset_at = None
        self.last_voiced_at = None
        self.stop_reason = "eof"
        self.stop_index = 0

    # -- per frame ---------------------------------------------------------

    def push(self, frame: bytes) -> Optional[bytes]:
        """Feed one frame. Returns the utterance when it is complete."""
        cfg = self.cfg
        if len(frame) != self.frame_bytes:
            return None
        self.frame_index += 1
        now = self.clock()
        if self.first_frame_at is None:
            self.first_frame_at = now

        level = _level_db(frame)

        # ---- smoothed level and stationarity -------------------------------
        if self.smooth_db is None:
            self.smooth_db = level
            self.prev_smooth_db = level
            is_flat = True
        else:
            change_db = abs(self.smooth_db - self.prev_smooth_db)
            is_flat = change_db <= cfg.vad_flatness_db
            self.smooth_db = self.smooth_db + cfg.vad_ema_alpha * (level - self.smooth_db)
            self.prev_smooth_db = self.smooth_db
        self.smooth_history.append(self.smooth_db)

        # ---- speech decision ------------------------------------------------
        if self.frame_index % self.reset_interval == 0:
            import webrtcvad as _webrtcvad

            self.vad = _webrtcvad.Vad(cfg.vad_aggressiveness)

        vad_speech = self.vad.is_speech(frame, cfg.sample_rate)

        # ---- background level -----------------------------------------------
        if self.min_level_db is None or level < self.min_level_db:
            self.min_level_db = level
        self.level_history.append(level)
        if len(self.level_history) >= 12:
            history = np.asarray(self.level_history)
            quiet = history[history <= self.min_level_db + cfg.vad_background_band_db]
            if len(quiet) >= 4:
                self.bg_db = float(np.median(quiet))
                self.background_known = True

        if vad_speech:
            self.signal_levels.append(level)
        if len(self.signal_levels) >= 10:
            self.speech_db = _percentile(self.signal_levels, 90)

        if self.background_known:
            snr_floor_db = self.bg_db + cfg.vad_noise_margin_db
        else:
            snr_floor_db = (
                self.min_level_db if self.min_level_db is not None else -100.0
            ) + cfg.vad_noise_margin_db
        # The VAD decides, the energy gate removes the frames that sit on
        # the noise floor. The gate is referenced to the true noise floor, so
        # it does not reject a quiet but real part of the speech. A frame
        # that is *not* flat is moving, so it is speech even when it is close
        # to the background: only a steady signal can be noise.
        is_voiced = vad_speech and (level > snr_floor_db or not is_flat)
        is_signal = level > snr_floor_db

        # ---- safety net for a VAD that is stuck on noise ---------------------
        local_bg = (
            float(np.median(np.asarray(self.level_history)))
            if len(self.level_history) >= 8
            else None
        )
        if (
            vad_speech
            and local_bg is not None
            and level <= local_bg + cfg.vad_noise_margin_db
            and _is_stationary(self.smooth_history, cfg)
        ):
            self.vad_conflict_frames += 1
        else:
            self.vad_conflict_frames = max(0, self.vad_conflict_frames - 1)
        noisy_room = (
            self.speech_db is not None
            and local_bg is not None
            and self.speech_db - local_bg < cfg.vad_snr_arm_db
        )
        self.vad_conflict_max = max(self.vad_conflict_max, self.vad_conflict_frames)
        if noisy_room and self.vad_conflict_frames >= cfg.vad_conflict_frames:
            self.vad_unreliable = True
            self.vad_conflict_frames = 0
        if self.vad_unreliable:
            is_voiced = level > (
                local_bg if local_bg is not None else snr_floor_db
            ) + cfg.vad_noise_margin_db

        is_quiet = (not is_voiced) and is_flat

        # ---- onset / speech -------------------------------------------------
        if not self.started:
            self.wait_ms += FRAME_MS
            self.pre_roll.append(frame)
            if is_voiced:
                self.voiced_run += 1
                self.voiced_run_levels.append(level)
                if self.voiced_run >= cfg.vad_start_frames:
                    rises_above_background = (
                        self.background_samples < 5
                        or max(self.voiced_run_levels)
                        > self.background_floor_db + cfg.vad_noise_margin_db
                    )
                    if rises_above_background:
                        self.started = True
                        self.onset_index = len(self.recorded) + self.voiced_run - cfg.vad_start_frames
                        self.onset_at = now
                        self.last_voiced_at = now
                        self.last_voiced_index = self.onset_index
                        self.recorded = list(self.pre_roll)
                        self.pre_roll.clear()
                        self.quiet_run_frames = 0
                    else:
                        self.voiced_run = 0
                        self.voiced_run_levels = []
            else:
                self.voiced_run = 0
                self.voiced_run_levels = []
                if self.background_samples < 60:
                    self.background_floor_db = min(self.background_floor_db, level)
                    self.background_samples += 1
            return None

        self.recorded.append(frame)
        self.speech_elapsed_ms += FRAME_MS
        if is_signal:
            self.signal_frames += 1
            self.last_signal_index = len(self.recorded) - 1
        if vad_speech:
            self.vad_speech_run += 1
        else:
            self.vad_speech_run = 0
        if is_voiced:
            self.vad_voiced_frames += 1
            self.last_voiced_at = now
            self.last_voiced_index = len(self.recorded) - 1
            self.quiet_run_frames = 0
        if is_quiet:
            self.quiet_frames += 1
            self.quiet_run_frames += 1

        if self.quiet_run_frames >= self.silence_frames:
            self.stop_reason = "silence"
            self.stop_at = now
            self.stop_index = len(self.recorded)
            return self._finish()
        if self.speech_elapsed_ms >= self.cfg.vad_max_seconds * 1000:
            self.stop_reason = "max_duration"
            self.stop_at = now
            self.stop_index = len(self.recorded)
            return self._finish()
        return None

    def _finish(self) -> bytes:
        end_index = len(self.recorded)
        if self.last_voiced_index is not None:
            end_index = min(end_index, self.last_voiced_index + 1 + self.keep_tail_frames)
        pcm = b"".join(self.recorded[:end_index])
        # The metrics have to be taken before the turn state is reset.
        self.last_metrics = self.metrics()
        self.last_metrics["kept_s"] = len(pcm) / 2 / self.cfg.sample_rate / self.cfg.channels
        self.reset_turn()
        return pcm

    def flush(self) -> Tuple[bytes, dict]:
        """End of stream: return a partial utterance or raise."""
        if not self.started:
            raise NoSpeechDetected("no speech detected")
        pcm = b"".join(self.recorded)
        metrics = self.metrics()
        self.reset_turn()
        return pcm, metrics

    def metrics(self) -> dict:
        onset = self.onset_index
        last_voiced = self.last_voiced_index
        wall_span = 0.0
        if self.first_frame_at is not None and self.stop_at is not None:
            wall_span = self.stop_at - self.first_frame_at
        return {
            "wait_s": (self.onset_at - self.first_frame_at)
            if (self.onset_at and self.first_frame_at)
            else 0.0,
            "speech_s": ((last_voiced - onset + 1) * FRAME_MS / 1000.0)
            if (last_voiced is not None and onset is not None)
            else 0.0,
            "signal_s": self.signal_frames * FRAME_MS / 1000.0,
            "vad_voiced_s": self.vad_voiced_frames * FRAME_MS / 1000.0,
            "quiet_s": self.quiet_frames * FRAME_MS / 1000.0,
            "trailing_silence_ms": max(0, (self.stop_index - (last_voiced or 0) - 1) * FRAME_MS),
            "end_of_speech_delay_ms": max(0, (self.stop_index - (last_voiced or 0)) * FRAME_MS),
            "end_of_speech_wall_s": (self.stop_at - self.last_voiced_at)
            if (self.stop_at and self.last_voiced_at)
            else 0.0,
            "wall_span_s": wall_span,
            "audio_span_s": len(self.recorded) * FRAME_MS / 1000.0,
            "pipeline_lag_s": max(0.0, wall_span - len(self.recorded) * FRAME_MS / 1000.0),
            "background_db": self.bg_db
            if self.bg_db is not None
            else (self.min_level_db if self.min_level_db is not None else -100.0),
            "speech_level_db": self.speech_db if self.speech_db is not None else -100.0,
            "energy_gate": bool(self.background_known),
            "stop_reason": self.stop_reason,
            "frames": len(self.recorded),
            "signal_frames": self.signal_frames,
            "vad_voiced_frames": self.vad_voiced_frames,
            "confirmed_frames": self.vad_voiced_frames,
            "vad_unreliable": bool(self.vad_unreliable),
            "vad_conflict_max": self.vad_conflict_max,
            "local_bg_db": -100.0,
            "snr_db": 0.0,
            "kept_s": 0.0,
        }
