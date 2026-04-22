"""Microphone capture via sounddevice.

The PortAudio stream opens in `__init__` and stays open for the lifetime of
the MicStream instance. `start()` / `stop()` gate whether incoming audio is
enqueued for ASR -- they do NOT open or close the stream. This eliminates
the 50-200 ms WASAPI startup cost that was clipping the first phonemes of
each recording.

On `start()`, we also seed the ASR queue with the last PRE_ROLL_MS of the
rolling ring buffer so audio spoken slightly before the hotkey press is
included. This covers human reaction time (people often start speaking a
beat before the key registers).
"""
from __future__ import annotations

import logging
import queue
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"
RECENT_SECONDS = 0.5          # ring buffer size (for overlay + pre-roll source)
PRE_ROLL_MS = 250             # prepended to each recording


class MicStream:
    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        pre_roll_ms: int = PRE_ROLL_MS,
    ) -> None:
        self.sample_rate = sample_rate
        self._pre_roll_samples = int(sample_rate * pre_roll_ms / 1000)

        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: sd.InputStream | None = None
        self.current_level: float = 0.0
        self._level_smooth = 0.35

        self._recent = np.zeros(int(sample_rate * RECENT_SECONDS), dtype=np.float32)
        self._recent_lock = threading.Lock()

        self._recording = False
        self._recording_lock = threading.Lock()

        self._open_stream()

    # --------------------------------------------------------------- stream
    def _open_stream(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=self._callback,
        )
        self._stream.start()
        log.info("mic stream opened @ %d Hz", self.sample_rate)

    def _callback(self, indata, frames, time_info, status) -> None:
        data = indata.copy().reshape(-1)

        # Always-on: rolling ring buffer (overlay + pre-roll source)
        with self._recent_lock:
            n = data.size
            if n >= self._recent.size:
                self._recent[:] = data[-self._recent.size:]
            else:
                self._recent[:-n] = self._recent[n:]
                self._recent[-n:] = data

        # Always-on: smoothed log-scale VU level
        rms = float(np.sqrt(np.mean(data * data) + 1e-12))
        norm = max(0.0, min(1.0, (np.log10(rms + 1e-6) + 4.0) / 3.5))
        self.current_level = (
            self._level_smooth * norm
            + (1.0 - self._level_smooth) * self.current_level
        )

        # Only enqueue for ASR when recording
        if self._recording:
            self._q.put(data)

    # --------------------------------------------------------- session API
    def start(self) -> None:
        """Begin a recording session. Seeds the queue with pre-roll audio."""
        with self._recording_lock:
            if self._recording:
                return
            # Clear any leftovers from prior sessions (shouldn't exist, but be safe)
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            # Seed with the last PRE_ROLL_MS of the ring buffer
            if self._pre_roll_samples > 0:
                with self._recent_lock:
                    seed = self._recent[-self._pre_roll_samples:].copy()
                self._q.put(seed)
            self._recording = True

    def stop(self) -> None:
        """End the recording session. The PortAudio stream stays open."""
        with self._recording_lock:
            self._recording = False
            self.current_level = 0.0
            # Zero the ring buffer so the tail of this session can't bleed into
            # the pre-roll seed of the next one if the user presses again fast.
            with self._recent_lock:
                self._recent[:] = 0.0

    def close(self) -> None:
        """Final teardown. Call on app exit."""
        with self._recording_lock:
            self._recording = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.error("mic close failed: %s", e)
            finally:
                self._stream = None

    # --------------------------------------------------------- consumer API
    def drain(self) -> np.ndarray:
        chunks: list[np.ndarray] = []
        while True:
            try:
                chunks.append(self._q.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(chunks)

    def get_recent(self) -> np.ndarray:
        with self._recent_lock:
            return self._recent.copy()
