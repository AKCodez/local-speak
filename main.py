"""Hold Right Ctrl to record, release to transcribe once and type.

Entry point for the launchable app. Sets up logging, tray icon, keyboard
listener, overlay, and the dictation lifecycle.

Quit: Right-click tray -> Quit, or Ctrl+Alt+Q anywhere.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

# Ensure working dir is the script's dir so imports + file lookups work the
# same whether launched from a shortcut, Explorer, or the registry Run key.
os.chdir(Path(__file__).resolve().parent)

import numpy as np
from pynput import keyboard

import logutil
from asr import ASR
from audio import MicStream
from inject import Typist
from overlay import Overlay
from tray import Tray

log = logging.getLogger(__name__)

HOTKEY = keyboard.Key.ctrl_r
QUIT_COMBO = "<ctrl>+<alt>+q"
SAMPLE_RATE = 16_000
MIN_AUDIO_SAMPLES = SAMPLE_RATE // 4  # 0.25 s -- skip accidental taps


class Dictation:
    def __init__(self, overlay: Overlay) -> None:
        log.info("Loading faster-whisper (large-v3-turbo) on CUDA...")
        self.asr = ASR("large-v3-turbo")
        self.typist = Typist()
        self.mic = MicStream()
        self.overlay = overlay
        self.active = False
        self.paused = False
        self._finalizing = False
        self._lock = threading.Lock()
        self.exit_event = threading.Event()
        log.info("Ready. Hold Right Ctrl to record. Ctrl+Alt+Q to quit.")

    def on_press(self, key) -> None:
        if key != HOTKEY:
            return
        with self._lock:
            if self.paused or self.active or self._finalizing:
                return
            self.active = True
            self.mic.start()
        self.overlay.call_on_ui(self.overlay.set_status, "Listening", True)
        self.overlay.call_on_ui(self.overlay.show)
        log.info("listening")

    def on_release(self, key) -> None:
        if key != HOTKEY:
            return
        with self._lock:
            if not self.active:
                return
            self.active = False
            self._finalizing = True
        self.mic.stop()
        audio = self.mic.drain()
        self.overlay.call_on_ui(self.overlay.set_status, "Transcribing...", False)
        threading.Thread(target=self._finalize, args=(audio,), daemon=True).start()

    def _finalize(self, audio: np.ndarray) -> None:
        try:
            if audio.size < MIN_AUDIO_SAMPLES:
                log.info("skipped (%.2fs < 0.25s)", audio.size / SAMPLE_RATE)
                return
            transcript = self.asr.transcribe(audio).strip()
            if not transcript:
                log.info("empty transcript")
                return
            self.typist.type(transcript + " ")
            log.info("typed: %r", transcript)
        except Exception:
            log.exception("transcribe error")
        finally:
            self.overlay.call_on_ui(self.overlay.hide)
            with self._lock:
                self._finalizing = False

    def set_paused(self, value: bool) -> None:
        with self._lock:
            self.paused = bool(value)

    def request_exit(self, reason: str) -> None:
        if self.exit_event.is_set():
            return
        log.info("quit (%s)", reason)
        self.exit_event.set()
        self.overlay.request_quit()

    def shutdown(self) -> None:
        try:
            self.mic.close()
        except Exception:
            pass


def main() -> None:
    logutil.configure()
    log.info("==== STT startup ====")

    overlay = Overlay()
    d = Dictation(overlay)
    overlay._level_provider = lambda: d.mic.current_level
    overlay._samples_provider = lambda: d.mic.get_recent()
    overlay.set_close_callback(lambda: d.request_exit("window closed"))

    tray = Tray(
        on_quit=lambda: d.request_exit("tray"),
        on_pause_toggle=d.set_paused,
        is_paused=lambda: d.paused,
    )
    tray.start()

    quit_hotkey = keyboard.GlobalHotKeys({
        QUIT_COMBO: lambda: d.request_exit("Ctrl+Alt+Q"),
    })
    quit_hotkey.start()

    dictate_listener = keyboard.Listener(on_press=d.on_press, on_release=d.on_release)
    dictate_listener.start()

    signal.signal(signal.SIGINT, lambda *_: d.request_exit("SIGINT"))
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: d.request_exit("SIGBREAK"))

    try:
        overlay.mainloop()
    except KeyboardInterrupt:
        d.request_exit("KeyboardInterrupt")
    finally:
        dictate_listener.stop()
        quit_hotkey.stop()
        tray.stop()
        d.shutdown()
    log.info("==== STT exit ====")


if __name__ == "__main__":
    main()
