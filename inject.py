"""Inject transcripts into the focused window via clipboard-paste.

Why clipboard, not per-character typing?

When Right Ctrl is released and we start typing immediately, the foreground
window's per-thread key-state table may still flag Ctrl as down until the
app processes its own WM_KEYUP -- which happens asynchronously and we have
no way to wait for. During that window, typed letters get re-interpreted
as Ctrl-shortcuts (Ctrl+T, Ctrl+P, Ctrl+Y, ...) and eaten by the app.

Clipboard + explicit Ctrl+V sidesteps this:
  - We hold the clipboard text ourselves, so the letters never traverse the
    keyboard-scan-code / virtual-key path.
  - The Ctrl we send is deliberate and matched with V, so there's no
    modifier-alignment race.
  - The previous clipboard content is saved and restored so the user's
    clipboard isn't clobbered.
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

# ---------- Win32 SendInput plumbing ----------
ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUT_UNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUT_UNION))


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002

_VK_LCONTROL = 0xA2
_VK_RCONTROL = 0xA3
_VK_CONTROL = 0x11
_VK_V = 0x56

_user32 = ctypes.windll.user32
_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short

# ---------- Clipboard plumbing ----------
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

_kernel32 = ctypes.windll.kernel32
_kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
_kernel32.GlobalSize.restype = ctypes.c_size_t

_user32.OpenClipboard.argtypes = (wintypes.HWND,)
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = (wintypes.UINT,)
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
_user32.SetClipboardData.restype = wintypes.HANDLE

CTRL_WAIT_DEADLINE_S = 0.5
POST_RELEASE_SETTLE_S = 0.05
POST_PASTE_WAIT_S = 0.10  # give the target app time to consume Ctrl+V before restore


def _ctrl_is_physically_down() -> bool:
    return bool(_user32.GetAsyncKeyState(_VK_RCONTROL) & 0x8000) or \
           bool(_user32.GetAsyncKeyState(_VK_LCONTROL) & 0x8000)


def _open_clipboard_with_retry(retries: int = 10) -> bool:
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _read_clipboard_unicode() -> str | None:
    if not _open_clipboard_with_retry():
        return None
    try:
        h = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return None
        ptr = _kernel32.GlobalLock(h)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def _write_clipboard_unicode(text: str) -> bool:
    if not _open_clipboard_with_retry():
        return False
    try:
        _user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        h_mem = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not h_mem:
            return False
        ptr = _kernel32.GlobalLock(h_mem)
        ctypes.memmove(ptr, buf, size)
        _kernel32.GlobalUnlock(h_mem)
        return bool(_user32.SetClipboardData(_CF_UNICODETEXT, h_mem))
    finally:
        _user32.CloseClipboard()


def _send_key(vk: int, up: bool) -> None:
    inp = _INPUT(type=_INPUT_KEYBOARD)
    flags = _KEYEVENTF_KEYUP if up else 0
    inp.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _paste_via_ctrl_v() -> None:
    # Ctrl down, V down, V up, Ctrl up -- as one batch
    events = (_INPUT * 4)()
    for i, (vk, up) in enumerate(
        [(_VK_CONTROL, False), (_VK_V, False), (_VK_V, True), (_VK_CONTROL, True)]
    ):
        events[i] = _INPUT(type=_INPUT_KEYBOARD)
        events[i].ki = _KEYBDINPUT(
            wVk=vk, wScan=0,
            dwFlags=_KEYEVENTF_KEYUP if up else 0,
            time=0, dwExtraInfo=0,
        )
    _user32.SendInput(4, events, ctypes.sizeof(_INPUT))


class Typist:
    def type(self, text: str) -> None:
        if not text:
            return

        # Wait for physical Ctrl release so our own Ctrl+V doesn't land on
        # top of the user's still-held key.
        deadline = time.monotonic() + CTRL_WAIT_DEADLINE_S
        while _ctrl_is_physically_down() and time.monotonic() < deadline:
            time.sleep(0.01)

        # Actively clear any stale modifier in the foreground thread's view.
        _send_key(_VK_LCONTROL, up=True)
        _send_key(_VK_RCONTROL, up=True)
        time.sleep(POST_RELEASE_SETTLE_S)

        # Save the existing clipboard so we can restore it.
        saved = _read_clipboard_unicode()

        if not _write_clipboard_unicode(text):
            log.error("clipboard write failed; transcript dropped")
            return

        _paste_via_ctrl_v()
        time.sleep(POST_PASTE_WAIT_S)

        # Best-effort restore. If the user's previous clipboard wasn't text
        # (image, file list, ...), we leave the transcript on the clipboard.
        if saved is not None:
            _write_clipboard_unicode(saved)
