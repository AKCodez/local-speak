"""Type text into the focused window via pynput."""
from __future__ import annotations

from pynput.keyboard import Controller


class Typist:
    def __init__(self) -> None:
        self._kb = Controller()

    def type(self, text: str) -> None:
        if not text:
            return
        self._kb.type(text)
