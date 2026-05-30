"""Hold Right Ctrl to record, release to transcribe once and type.

Entry point for the launchable app. Sets up logging, tray icon, keyboard
listener, overlay, and the dictation lifecycle.

Quit: Right-click tray -> Quit, or Ctrl+Alt+Q anywhere.

Exit codes (read by run.vbs supervisor):
  0  = clean user-initiated exit, do not relaunch.
  42 = self-restart requested (keyboard hooks unrecoverable, etc.).
"""
from __future__ import annotations

import ctypes
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

EXIT_CLEAN = 0
EXIT_SELF_RESTART = 42

# Ensure working dir is the script's dir so imports + file lookups work the
# same whether launched from a shortcut, Explorer, or the registry Run key.
os.chdir(Path(__file__).resolve().parent)

import numpy as np
from pynput import keyboard

import logutil
from asr import ASR
from audio import MicStream
from hotkey import HotkeyManager
from inject import Typist
from overlay import Overlay
from tray import Tray

log = logging.getLogger(__name__)

HOTKEY = keyboard.Key.ctrl_r
QUIT_COMBO = "<ctrl>+<alt>+q"
SAMPLE_RATE = 16_000
MIN_AUDIO_SAMPLES = SAMPLE_RATE // 4  # 0.25 s -- skip accidental taps

# Stale-recording guard. If a hook rebuild lands between a press and its
# release, on_release is never delivered and `active` stays True forever,
# silently ignoring all future presses. A watchdog watches the physical key
# and force-finalizes if we're "active" while Right Ctrl is physically up.
_VK_RCONTROL = 0xA3
STALE_ACTIVE_S = 0.4

# Cold-boot self-heal. When Windows autostarts us during the post-boot
# thrash (Defender scan, GPU/USB init), the keyboard hook and mic often come
# up degraded; a relaunch once the system has settled reliably fixes it. So
# if we started within BOOT_WINDOW_S of boot, schedule one restart at
# BOOT_SETTLE_UPTIME_S of uptime. BOOT_SETTLE_UPTIME_S must exceed
# BOOT_WINDOW_S so the relaunched process doesn't reschedule and loop.
#
# The window is generous: on a busy boot the autostart can fire 90-150s in
# (a real miss was logged at 99s uptime), so a tight window silently skips
# the heal. Better to occasionally restart a healthy process than to never
# heal a degraded one.
BOOT_WINDOW_S = 180
BOOT_SETTLE_UPTIME_S = 210


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
        self.exit_code = EXIT_CLEAN
        threading.Thread(
            target=self._stale_active_watchdog, daemon=True,
            name="stale-active-watchdog",
        ).start()
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
                return
            transcript = self.asr.transcribe(audio).strip()
            if not transcript:
                return
            self.typist.type(transcript + " ")
        except Exception:
            log.exception("transcribe error")
        finally:
            self.overlay.call_on_ui(self.overlay.hide)
            with self._lock:
                self._finalizing = False

    def _stale_active_watchdog(self) -> None:
        """Recover from a missed key-release. If on_release never arrives
        (a hook rebuild can drop it), `active` is stuck True and every later
        press is ignored. When we're active but Right Ctrl has been
        physically up for STALE_ACTIVE_S, synthesize the release -- this also
        salvages the transcript if the user actually spoke."""
        user32 = ctypes.windll.user32
        up_since: float | None = None
        while not self.exit_event.wait(0.05):
            if not self.active:
                up_since = None
                continue
            if bool(user32.GetAsyncKeyState(_VK_RCONTROL) & 0x8000):
                up_since = None  # key physically held -- normal recording
                continue
            now = time.monotonic()
            if up_since is None:
                up_since = now
            elif now - up_since >= STALE_ACTIVE_S:
                up_since = None
                log.warning("recording release was missed; finalizing")
                self.on_release(HOTKEY)

    def set_paused(self, value: bool) -> None:
        with self._lock:
            self.paused = bool(value)

    def request_exit(self, reason: str, exit_code: int = EXIT_CLEAN) -> None:
        if self.exit_event.is_set():
            return
        log.info("quit (%s)", reason)
        self.exit_code = exit_code
        self.exit_event.set()
        self.overlay.request_quit()

    def shutdown(self) -> None:
        try:
            self.mic.close()
        except Exception:
            pass


def _schedule_cold_boot_restart(d: Dictation) -> None:
    """If we autostarted during the post-boot window, request one fresh-
    process restart once the system has settled (see BOOT_* constants)."""
    try:
        k32 = ctypes.WinDLL("kernel32")
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        uptime_s = k32.GetTickCount64() / 1000.0
    except Exception:
        return
    if uptime_s >= BOOT_WINDOW_S:
        return  # manual launch or debug relaunch, not a boot-time autostart
    delay = max(0.0, BOOT_SETTLE_UPTIME_S - uptime_s)
    log.info(
        "cold-boot start (uptime %.0fs); will restart fresh in %.0fs once the "
        "system settles", uptime_s, delay,
    )

    def _fire() -> None:
        if d.exit_event.wait(delay):
            return  # already exiting for another reason
        d.request_exit("cold-boot settle restart", exit_code=EXIT_SELF_RESTART)

    threading.Thread(target=_fire, daemon=True, name="cold-boot-restart").start()


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

    hotkeys = HotkeyManager(
        on_press=d.on_press,
        on_release=d.on_release,
        global_hotkeys={QUIT_COMBO: lambda: d.request_exit("Ctrl+Alt+Q")},
        on_giveup=lambda: d.request_exit(
            "hotkey hooks unrecoverable", exit_code=EXIT_SELF_RESTART
        ),
        on_suspend=lambda: d.request_exit(
            "resume from sleep", exit_code=EXIT_SELF_RESTART
        ),
    )
    hotkeys.start()
    _schedule_cold_boot_restart(d)

    signal.signal(signal.SIGINT, lambda *_: d.request_exit("SIGINT"))
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: d.request_exit("SIGBREAK"))

    try:
        overlay.mainloop()
    except KeyboardInterrupt:
        d.request_exit("KeyboardInterrupt")
    finally:
        hotkeys.stop()
        tray.stop()
        d.shutdown()
    log.info("==== STT exit (code=%d) ====", d.exit_code)
    # Use sys.exit so the supervisor sees the right code. Daemon threads
    # are torn down automatically.
    sys.exit(d.exit_code)


if __name__ == "__main__":
    main()
