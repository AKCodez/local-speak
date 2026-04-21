"""faster-whisper ASR wrapper. One-shot transcription, thread-safe."""
from __future__ import annotations

import re
import threading

import numpy as np
from faster_whisper import WhisperModel

_WS = re.compile(r"\s+")


class ASR:
    def __init__(self, model_name: str = "large-v3-turbo") -> None:
        self.model = WhisperModel(
            model_name,
            device="cuda",
            compute_type="float16",
        )
        self._lock = threading.Lock()

    def transcribe(self, audio: np.ndarray, language: str = "en") -> str:
        if audio.size == 0:
            return ""
        with self._lock:
            segments, _info = self.model.transcribe(
                audio,
                language=language,
                vad_filter=False,
                # beam_size=1 was losing short words like "I" at chunk boundaries.
                # 5 is Whisper's default and materially improves small-word recall.
                beam_size=5,
                best_of=5,
                temperature=0.0,
                condition_on_previous_text=False,
            )
            text = "".join(seg.text for seg in segments)
        return _WS.sub(" ", text).strip()
