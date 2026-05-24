"""Resilient keyboard-hook wrapper.

Windows silently disables WH_KEYBOARD_LL hooks in several scenarios:
  * The system suspended (sleep / hibernate / fast user switching).
  * The user's session was locked and unlocked.
  * LowLevelHooksTimeout was exceeded -- e.g. our callback held the GIL
    too long during model warmup.
  * On a fresh boot, services / Defender / GPU init starve the hook so
    SetWindowsHookEx succeeds but no events are ever delivered.

When that happens pynput's Listener thread is technically alive
(parked in GetMessage), `running` is True, but no keyboard events ever
arrive. The user observes "Right Ctrl does nothing" with no error.

Mitigation, in order of how it triggers:

  1. Startup self-test. After _build(), we synthesise a harmless key
     (F24, unmapped in normal apps) via SendInput and verify our
     listener saw it. If not, rebuild and retry. Loops until the hook
     proves itself working. This catches the cold-boot case.

  2. Live deafness probe. The watchdog compares the wall-clock age of
     the user's last input (per Windows GetLastInputInfo) against our
     listener's last-seen-event timestamp. If the user has been
     actively typing within the last few seconds and our listener has
     seen nothing for tens of seconds, the hook is dead and we rebuild.

  3. Explicit resume signals. A hidden message-only window subscribes
     to WM_POWERBROADCAST (PBT_APMRESUMESUSPEND/AUTOMATIC) and
     WM_WTSSESSION_CHANGE (WTS_SESSION_UNLOCK) so we get an immediate
     rebuild on suspend resume / session unlock.

  4. Heuristic suspend probe. The watchdog also rebuilds when the
     monotonic clock vs intended wait disagree by more than a threshold
     -- backstop for systems where the explicit power notification is
     missed.

  5. Periodic refresh every 5 minutes as belt-and-braces.

  6. _teardown joins the old listener thread so its hook handle is
     actually released before the new hook is installed -- otherwise
     stale hooks accumulate in the chain and clog event delivery.
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

# Self-test (post-build): send a synthetic key, see if the listener notices.
SELF_TEST_DELAY_S = 0.25
SELF_TEST_RETRIES = 6
SELF_TEST_BACKOFF_S = 0.5
_VK_F24 = 0x87  # rare; not bound to anything in normal apps

# Deafness probe: watch the physical up->down EDGE of Right Ctrl via
# GetAsyncKeyState. A working hook fires our listener's on_press for ctrl_r
# within a few tens of ms of that edge. If the edge happens but the listener
# never reports the matching press, the hook is genuinely deaf.
#
# We MUST key off the edge, not "the listener has gone quiet": a held
# modifier key emits no events at all between press and release on a
# perfectly working hook (Ctrl doesn't auto-repeat), so any "listener silent
# for N ms" check pathologically self-triggers on every normal hold and the
# resulting rebuild churn is what actually breaks dictation.
PROBE_INTERVAL_S = 0.015        # poll Right Ctrl every 15 ms
PRESS_GRACE_S = 0.12            # listener must report the press within this

# After this many *separate* user holds confirm the hook is deaf, we give
# up the in-process recovery and ask the supervisor to relaunch with a
# fresh process. Each confirmation requires the user to deliberately hold
# the key, so 3 is a strong, user-driven signal (not a clock-driven loop).
DEAFNESS_GIVEUP_THRESHOLD = 3

# ---------- Win32 plumbing ----------
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

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_VK_RCONTROL = 0xA3

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
_kernel32.GetTickCount.restype = wintypes.DWORD
_wtsapi32.WTSRegisterSessionNotification.argtypes = (wintypes.HWND, wintypes.DWORD)
_wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL


# ---------- SendInput plumbing for the synthetic self-test key ----------
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


# Use a private WinDLL handle for SendInput so our argtypes don't clobber
# inject.py's (both modules set argtypes for the same exported function;
# whichever loads last wins, breaking the other one).
_user32_si = ctypes.WinDLL("user32")
_user32_si.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32_si.SendInput.restype = wintypes.UINT


# ---------- GetLastInputInfo ----------
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


_user32.GetLastInputInfo.argtypes = (ctypes.POINTER(_LASTINPUTINFO),)
_user32.GetLastInputInfo.restype = wintypes.BOOL


def _last_user_input_age_s() -> float:
    """How long ago (seconds) Windows received any user input. Inf if the
    call fails."""
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO), dwTime=0)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return float("inf")
    # Both GetTickCount and dwTime wrap every ~49 days; subtraction is
    # well-defined modulo 2**32 thanks to wraparound semantics.
    delta_ms = (_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return delta_ms / 1000.0


def _send_synthetic(vk: int) -> None:
    arr = (_INPUT * 2)()
    arr[0] = _INPUT(type=_INPUT_KEYBOARD)
    arr[0].ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
    arr[1] = _INPUT(type=_INPUT_KEYBOARD)
    arr[1].ki = _KEYBDINPUT(
        wVk=vk, wScan=0, dwFlags=_KEYEVENTF_KEYUP, time=0, dwExtraInfo=0
    )
    _user32_si.SendInput(
        2, ctypes.cast(arr, ctypes.POINTER(_INPUT)), ctypes.sizeof(_INPUT)
    )


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
    """Listens for resume-from-sleep and session-unlock from Windows."""

    def __init__(
        self,
        on_suspend_resume: Callable[[], None],
        on_session_unlock: Callable[[], None],
    ) -> None:
        self._on_suspend_resume = on_suspend_resume
        self._on_session_unlock = on_session_unlock
        self._hwnd: int = 0
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
                self._on_suspend_resume()
            except Exception:
                log.exception("power notifier callback failed")
            return 1
        if msg == _WM_WTSSESSION_CHANGE and wparam == _WTS_SESSION_UNLOCK:
            try:
                self._on_session_unlock()
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
        on_giveup: Callable[[], None] | None = None,
        on_suspend: Callable[[], None] | None = None,
    ) -> None:
        self._user_on_press = on_press
        self._user_on_release = on_release
        self._global_hotkeys = global_hotkeys
        self._on_giveup = on_giveup
        self._on_suspend = on_suspend
        self._listener: keyboard.Listener | None = None
        self._global: keyboard.GlobalHotKeys | None = None
        self._built_at: float = 0.0
        self._stop = threading.Event()
        self._build_lock = threading.Lock()
        self._wd_thread: threading.Thread | None = None
        self._power: _PowerNotifier | None = None
        # Liveness tracking
        self._listener_rctrl_press_at: float = 0.0
        self._self_test_seen = threading.Event()
        self._deafness_count = 0
        self._gave_up = False

    def start(self) -> None:
        self._build()
        self._wd_thread = threading.Thread(
            target=self._watchdog, daemon=True, name="hotkey-watchdog"
        )
        self._wd_thread.start()
        threading.Thread(
            target=self._deafness_probe, daemon=True, name="hotkey-deaf-probe"
        ).start()
        self._power = _PowerNotifier(
            on_suspend_resume=self._handle_suspend,
            on_session_unlock=lambda: self._on_external_resume("session unlock"),
        )
        self._power.start()
        # Run the cold-boot self-test off the main thread so startup
        # ordering (overlay.mainloop) isn't held up.
        threading.Thread(
            target=self._run_self_test, daemon=True, name="hotkey-self-test"
        ).start()

    def stop(self) -> None:
        self._stop.set()
        self._teardown()

    # ---------- event wrappers (track liveness, then forward) ----------
    def _on_press_wrapped(self, key) -> None:
        self._deafness_count = 0  # any received event proves the hook works
        if self._is_self_test_key(key):
            self._self_test_seen.set()
            return  # don't forward; user code never sees the F24 probe
        if key == keyboard.Key.ctrl_r:
            self._listener_rctrl_press_at = time.monotonic()
        try:
            self._user_on_press(key)
        except Exception:
            log.exception("user on_press raised")

    def _on_release_wrapped(self, key) -> None:
        self._deafness_count = 0
        if self._is_self_test_key(key):
            return
        try:
            self._user_on_release(key)
        except Exception:
            log.exception("user on_release raised")

    @staticmethod
    def _is_self_test_key(key) -> bool:
        # pynput maps F24 to keyboard.Key.f24 on Windows; vk is also exposed.
        try:
            if getattr(key, "vk", None) == _VK_F24:
                return True
        except Exception:
            pass
        return key == getattr(keyboard.Key, "f24", None)

    # ---------- external triggers ----------
    def _handle_suspend(self) -> None:
        """Resume from sleep. Prefer a full fresh-process restart (the
        supervisor relaunches us) over an in-process rebuild: a fresh
        process re-inits every subsystem -- hooks, mic, PortAudio device
        list -- with no stale clocks or device handles, which is the only
        thing that has proven reliable across wake. Falls back to an
        in-process rebuild if no restart hook is wired."""
        if self._on_suspend is not None:
            log.info("resume from suspend; requesting fresh-process restart")
            try:
                self._on_suspend()
            except Exception:
                log.exception("on_suspend callback raised")
            return
        self._on_external_resume("resume from suspend")

    def _on_external_resume(self, reason: str) -> None:
        self._rebuild(reason)
        # Re-verify hooks work after a resume rebuild, in case the OS state
        # is still settling.
        threading.Thread(
            target=self._run_self_test, daemon=True, name="hotkey-self-test"
        ).start()

    # ---------- core build/teardown ----------
    def _build(self) -> None:
        self._self_test_seen.clear()
        self._global = keyboard.GlobalHotKeys(self._global_hotkeys)
        self._global.start()
        self._listener = keyboard.Listener(
            on_press=self._on_press_wrapped, on_release=self._on_release_wrapped
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
                log.warning(
                    "old hotkey listener didn't exit within %.1fs",
                    TEARDOWN_JOIN_S,
                )
        self._listener = None
        self._global = None

    def _rebuild(self, reason: str) -> None:
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

    # ---------- self-test ----------
    def _run_self_test(self) -> None:
        # Give the freshly-built hook a moment to register at the kernel
        # level before we probe it.
        time.sleep(SELF_TEST_DELAY_S)
        for attempt in range(1, SELF_TEST_RETRIES + 1):
            if self._stop.is_set():
                return
            self._self_test_seen.clear()
            try:
                _send_synthetic(_VK_F24)
            except Exception:
                log.exception("self-test SendInput failed")
                return
            if self._self_test_seen.wait(timeout=0.5):
                if attempt > 1:
                    log.info("hotkey self-test passed on attempt %d", attempt)
                return
            log.warning(
                "hotkey self-test attempt %d/%d: no event seen, rebuilding",
                attempt, SELF_TEST_RETRIES,
            )
            self._rebuild(f"self-test failed (attempt {attempt})")
            time.sleep(SELF_TEST_BACKOFF_S)
        log.error("hotkey self-test exhausted retries; hook may be deaf")

    # ---------- watchdog ----------
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
                if self._on_suspend is not None:
                    log.info(
                        "suspend/resume detected (gap=%.1fs); requesting "
                        "fresh-process restart", gap,
                    )
                    try:
                        self._on_suspend()
                    except Exception:
                        log.exception("on_suspend callback raised")
                    return  # process is exiting; stop the watchdog
                self._rebuild(f"suspend/resume detected (gap={gap:.1f}s)")
                continue
            if not self._alive():
                self._rebuild("listener died")
                continue
            if now - self._built_at > FORCE_REBUILD_S:
                self._rebuild("periodic refresh")

    def _deafness_probe(self) -> None:
        """Watch the physical up->down edge of Right Ctrl. A working hook
        reports the matching listener press within PRESS_GRACE_S; if the
        edge happens and no press is ever reported, the hook is deaf.

        Keying off the edge (not "listener has gone quiet") is essential: a
        held modifier emits no events between press and release on a
        perfectly working hook, so a quiet-listener check self-triggers on
        every normal hold."""
        was_down = False
        edge_at: float | None = None
        signaled = False
        while not self._stop.wait(PROBE_INTERVAL_S):
            if self._gave_up:
                return
            is_down = bool(_user32.GetAsyncKeyState(_VK_RCONTROL) & 0x8000)
            if is_down and not was_down:
                edge_at = time.monotonic()  # user just pressed Right Ctrl
                signaled = False
            elif not is_down:
                edge_at = None
                signaled = False
            was_down = is_down
            if edge_at is None or signaled:
                continue
            # The real press happened up to one poll interval before we
            # noticed the edge; accept a listener press from slightly before.
            if self._listener_rctrl_press_at >= edge_at - PROBE_INTERVAL_S - 0.02:
                edge_at = None  # listener saw the press -- hook is working
                continue
            waited = time.monotonic() - edge_at
            if waited >= PRESS_GRACE_S:
                signaled = True
                self._handle_deafness(waited)

    def _handle_deafness(self, waited: float) -> None:
        self._deafness_count += 1
        log.warning(
            "deafness confirmed %d/%d: Right Ctrl pressed but listener saw "
            "no matching event after %.2fs",
            self._deafness_count, DEAFNESS_GIVEUP_THRESHOLD, waited,
        )
        self._rebuild(f"deafness {self._deafness_count}/{DEAFNESS_GIVEUP_THRESHOLD}")
        # Verify the rebuild actually produced a working hook. If self-test
        # fails, _run_self_test will retry-rebuild several times and finally
        # log a giveup error. That gives us much faster recovery than
        # waiting for the user to hold Right Ctrl again.
        threading.Thread(
            target=self._run_self_test,
            daemon=True,
            name="hotkey-self-test-post-deafness",
        ).start()
        if (
            self._deafness_count >= DEAFNESS_GIVEUP_THRESHOLD
            and not self._gave_up
            and self._on_giveup is not None
        ):
            self._gave_up = True
            log.error(
                "hotkey hooks unrecoverable after %d confirmed holds; "
                "signaling supervisor for fresh-process restart",
                self._deafness_count,
            )
            try:
                self._on_giveup()
            except Exception:
                log.exception("on_giveup callback raised")
