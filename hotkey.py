"""Resilient keyboard-hook wrapper.

Windows silently disables WH_KEYBOARD_LL hooks in several scenarios:
  * The system suspended (sleep / hibernate / fast user switching).
  * The user's session was locked and unlocked.
  * LowLevelHooksTimeout was exceeded -- e.g. our callback held the GIL
    too long during model warmup.

When that happens pynput's Listener thread is technically still alive
(parked in GetMessage with a dead hook handle), but no keyboard events
ever arrive. The user observes "Right Ctrl does nothing" with no error
in the log.

Mitigation:
  1. A hidden message-only window subscribes to WM_POWERBROADCAST
     (PBT_APMRESUMESUSPEND) and WM_WTSSESSION_CHANGE (SESSION_UNLOCK)
     so we get an explicit signal from Windows on resume/unlock and
     rebuild the hooks immediately.
  2. A watchdog thread also rebuilds when the monotonic clock vs the
     watchdog's intended wait length disagree (catches sleep on
     systems where the explicit power notification is missed) or when
     a pynput thread stopped running.
  3. Periodic refresh every 5 minutes as belt-and-braces.
  4. _teardown joins the old listener thread so its hook handle is
     actually released before a new hook is installed -- previously
     dead hooks accumulated in the chain after each rebuild.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable

from pynput import keyboard

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 5.0
SUSPEND_GAP_S = 20.0
FORCE_REBUILD_S = 5 * 60.0     # belt-and-braces: rebuild every 5 minutes
TEARDOWN_JOIN_S = 1.5          # seconds we wait for the old listener thread

# ---------- Win32 plumbing for resume/unlock notifications ----------
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_wtsapi32 = ctypes.WinDLL("Wtsapi32.dll")

_HWND_MESSAGE = wintypes.HWND(-3)
_WM_POWERBROADCAST = 0x0218
_WM_WTSSESSION_CHANGE = 0x02B1
_PBT_APMRESUMESUSPEND = 0x0007
_PBT_APMRESUMEAUTOMATIC = 0x0012
_WTS_SESSION_UNLOCK = 0x8
_NOTIFY_FOR_THIS_SESSION = 0

# LRESULT is LONG_PTR — pointer-sized on x64. Using a plain c_long would
# truncate the return value and corrupt the message pump on 64-bit.
_LRESULT = ctypes.c_ssize_t

_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

_user32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.RegisterClassW.argtypes = (ctypes.c_void_p,)
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.DefWindowProcW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)
_user32.DefWindowProcW.restype = _LRESULT
_user32.GetMessageW.argtypes = (
    ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT,
)
_user32.GetMessageW.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = (ctypes.c_void_p,)
_user32.TranslateMessage.restype = wintypes.BOOL
_user32.DispatchMessageW.argtypes = (ctypes.c_void_p,)
_user32.DispatchMessageW.restype = _LRESULT
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_wtsapi32.WTSRegisterSessionNotification.argtypes = (wintypes.HWND, wintypes.DWORD)
_wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _PowerNotifier:
    """Listens for resume-from-sleep and session-unlock from Windows.

    Owns its own thread + message-only window. On either event, calls
    on_resume(reason) on the watchdog's thread (the callback is invoked
    inline from the wndproc, so callers should keep the work cheap or
    dispatch it themselves).
    """

    def __init__(self, on_resume: Callable[[str], None]) -> None:
        self._on_resume = on_resume
        self._hwnd: int = 0
        self._stop = False
        self._wndproc_ref = _WNDPROC(self._wndproc)  # keep ref alive
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="hotkey-power-notifier"
        )

    def start(self) -> None:
        self._thread.start()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_POWERBROADCAST and wparam in (
            _PBT_APMRESUMESUSPEND, _PBT_APMRESUMEAUTOMATIC
        ):
            try:
                self._on_resume("resume from suspend")
            except Exception:
                log.exception("power notifier callback failed")
            return 1
        if msg == _WM_WTSSESSION_CHANGE and wparam == _WTS_SESSION_UNLOCK:
            try:
                self._on_resume("session unlock")
            except Exception:
                log.exception("session notifier callback failed")
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        cls = _WNDCLASS()
        cls.lpfnWndProc = self._wndproc_ref
        cls.hInstance = _kernel32.GetModuleHandleW(None)
        cls.lpszClassName = "STTPowerNotifierWnd"
        atom = _user32.RegisterClassW(ctypes.byref(cls))
        if not atom:
            log.error("RegisterClassW failed: %d", _kernel32.GetLastError())
            return
        self._hwnd = _user32.CreateWindowExW(
            0, cls.lpszClassName, "stt-power-notifier", 0, 0, 0, 0, 0,
            _HWND_MESSAGE, 0, cls.hInstance, 0,
        )
        if not self._hwnd:
            log.error("CreateWindowExW failed: %d", _kernel32.GetLastError())
            return
        if not _wtsapi32.WTSRegisterSessionNotification(
            self._hwnd, _NOTIFY_FOR_THIS_SESSION
        ):
            log.warning(
                "WTSRegisterSessionNotification failed: %d",
                _kernel32.GetLastError(),
            )

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))


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
        self._build_lock = threading.Lock()
        self._wd_thread: threading.Thread | None = None
        self._power: _PowerNotifier | None = None

    def start(self) -> None:
        self._build()
        self._wd_thread = threading.Thread(
            target=self._watchdog, daemon=True, name="hotkey-watchdog"
        )
        self._wd_thread.start()
        self._power = _PowerNotifier(on_resume=self._on_external_resume)
        self._power.start()

    def stop(self) -> None:
        self._stop.set()
        self._teardown()

    def _on_external_resume(self, reason: str) -> None:
        # Called from the power-notifier thread on resume/unlock. Rebuild
        # immediately rather than waiting for the next watchdog tick.
        self._rebuild(reason)

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
            try:
                h.join(timeout=TEARDOWN_JOIN_S)
            except Exception:
                pass
            if h.is_alive():
                # Old listener thread didn't exit -- its hook handle is
                # likely still registered. We can't force-unhook without
                # private pynput internals, but logging tells us if this
                # starts happening regularly.
                log.warning("old hotkey listener didn't exit within %.1fs", TEARDOWN_JOIN_S)
        self._listener = None
        self._global = None

    def _rebuild(self, reason: str) -> None:
        # Serialise rebuilds so power notifier + watchdog can't race.
        if not self._build_lock.acquire(blocking=False):
            return
        try:
            log.info("hotkey rebuild (%s)", reason)
            self._teardown()
            try:
                self._build()
            except Exception:
                log.exception("hotkey rebuild failed")
        finally:
            self._build_lock.release()

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
