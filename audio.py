"""Microphone capture via sounddevice."""
from __future__ import annotations

import queue
import threading

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"
RECENT_SECONDS = 0.5  # how much audio the overlay can visualise


class MicStream:
    """Continuous 16 kHz mono mic stream.

    - `drain()` pulls everything queued since the last drain (for ASR).
    - `current_level` is a smoothed 0..1 VU reading, updated on every callback.
    - `get_recent()` returns the last ~RECENT_SECONDS of raw samples so the
      overlay can draw a live waveform.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: sd.InputStream | None = None
        self.current_level: float = 0.0
        self._level_smooth = 0.35

        self._recent = np.zeros(int(sample_rate * RECENT_SECONDS), dtype=np.float32)
        self._recent_lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status) -> None:
        data = indata.copy().reshape(-1)
        self._q.put(data)

        rms = float(np.sqrt(np.mean(data * data) + 1e-12))
        norm = max(0.0, min(1.0, (np.log10(rms + 1e-6) + 4.0) / 3.5))
        self.current_level = (
            self._level_smooth * norm
            + (1.0 - self._level_smooth) * self.current_level
        )

        with self._recent_lock:
            n = data.size
            if n >= self._recent.size:
                self._recent[:] = data[-self._recent.size:]
            else:
                # shift left by n, append new samples at the end
                self._recent[:-n] = self._recent[n:]
                self._recent[-n:] = data

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        self.current_level = 0.0
        with self._recent_lock:
            self._recent[:] = 0.0
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

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
