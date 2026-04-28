"""Resilient keyboard-hook wrapper.

Windows silently disables WH_KEYBOARD_LL hooks in several scenarios:
  * The system suspended (sleep / hibernate / fast user switching).
  * LowLevelHooksTimeout was exceeded -- e.g. our callback held the GIL
    too long during model warmup.

When that happens pynput's Listener thread is technically still alive
(blocked on GetMessage with a dead hook handle), but no keyboard events
ever arrive. The user observes "Right Ctrl does nothing" with no error
in the log.

Mitigation: a watchdog that rebuilds both the keyboard.Listener and the
keyboard.GlobalHotKeys instances when:
  1. Either pynput thread reports it stopped running.
  2. A wall-clock gap between two short waits proves the system slept.
  3. A maximum age (FORCE_REBUILD_S) elapsed since the last rebuild --
     belt-and-braces against silently disabled hooks we can't detect.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from pynput import keyboard

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 5.0
SUSPEND_GAP_S = 20.0
FORCE_REBUILD_S = 30 * 60.0  # proactively rebuild hooks every 30 minutes


class HotkeyManager:
    def __init__(
        self,
        on_press: Callable,
        on_release: Callable,
        global_hotkeys: dict[str, Callable],
    ) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._global_hotkeys = global_hotkeys
        self._listener: keyboard.Listener | None = None
        self._global: keyboard.GlobalHotKeys | None = None
        self._built_at: float = 0.0
        self._stop = threading.Event()
        self._wd_thread: threading.Thread | None = None

    def start(self) -> None:
        self._build()
        self._wd_thread = threading.Thread(
            target=self._watchdog, daemon=True, name="hotkey-watchdog"
        )
        self._wd_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._teardown()

    def _build(self) -> None:
        self._global = keyboard.GlobalHotKeys(self._global_hotkeys)
        self._global.start()
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()
        self._built_at = time.monotonic()

    def _teardown(self) -> None:
        for h in (self._listener, self._global):
            if h is None:
                continue
            try:
                h.stop()
            except Exception:
                pass
        self._listener = None
        self._global = None

    def _rebuild(self, reason: str) -> None:
        log.info("hotkey rebuild (%s)", reason)
        self._teardown()
        try:
            self._build()
        except Exception:
            log.exception("hotkey rebuild failed")

    def _alive(self) -> bool:
        return (
            self._listener is not None
            and self._listener.running
            and self._global is not None
            and self._global.running
        )

    def _watchdog(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(CHECK_INTERVAL_S):
            now = time.monotonic()
            gap = now - last
            last = now
            if gap > SUSPEND_GAP_S:
                self._rebuild(f"suspend/resume detected (gap={gap:.1f}s)")
                continue
            if not self._alive():
                self._rebuild("listener died")
                continue
            if now - self._built_at > FORCE_REBUILD_S:
                self._rebuild("periodic refresh")
