"""System tray icon with the admin-panel menu for STT.

Runs pystray on its own thread so the main thread stays free for Tk.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw

import autostart
import logutil

log = logging.getLogger(__name__)

ICON_SIZE = 64


def _make_icon() -> Image.Image:
    """Tiny cyan-on-navy mic glyph. Generated at runtime, no .ico file needed."""
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Navy circle + cyan ring
    d.ellipse(
        (2, 2, ICON_SIZE - 3, ICON_SIZE - 3),
        fill=(10, 22, 40, 255),
        outline=(56, 189, 248, 255),
        width=3,
    )
    # Mic body
    d.rounded_rectangle((24, 16, 40, 40), radius=8, fill=(56, 189, 248, 255))
    # Stand
    d.line((32, 40, 32, 50), fill=(56, 189, 248, 255), width=3)
    d.line((22, 50, 42, 50), fill=(56, 189, 248, 255), width=3)
    return img


class Tray:
    def __init__(
        self,
        on_quit: Callable[[], None],
        on_pause_toggle: Callable[[bool], None],
        is_paused: Callable[[], bool],
    ) -> None:
        self._on_quit = on_quit
        self._on_pause_toggle = on_pause_toggle
        self._is_paused = is_paused
        self._thread: threading.Thread | None = None

        menu = pystray.Menu(
            pystray.MenuItem("STT Dictation", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows",
                self._toggle_autostart,
                checked=lambda _: autostart.is_enabled(),
            ),
            pystray.MenuItem(
                "Pause dictation",
                self._toggle_pause,
                checked=lambda _: self._is_paused(),
            ),
            pystray.MenuItem("Open log folder", self._open_log_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self._icon = pystray.Icon(
            name="STT",
            icon=_make_icon(),
            title="STT Dictation",
            menu=menu,
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception as e:
            log.error("tray stop failed: %s", e)

    # ---------- menu handlers (pystray thread) ----------
    def _toggle_autostart(self, icon, item) -> None:
        new = not autostart.is_enabled()
        try:
            autostart.set_enabled(new)
            log.info("autostart %s", "enabled" if new else "disabled")
        except Exception as e:
            log.exception("autostart toggle failed: %s", e)

    def _toggle_pause(self, icon, item) -> None:
        new = not self._is_paused()
        self._on_pause_toggle(new)
        log.info("pause %s", "on" if new else "off")

    def _open_log_folder(self, icon, item) -> None:
        try:
            os.startfile(str(logutil.log_dir()))
        except Exception as e:
            log.error("open log folder failed: %s", e)

    def _quit(self, icon, item) -> None:
        log.info("tray quit")
        try:
            self._on_quit()
        finally:
            self.stop()
