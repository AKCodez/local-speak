"""Microphone capture via sounddevice.

The PortAudio stream opens in `__init__` and stays open for the lifetime of
the MicStream instance. `start()` / `stop()` gate whether incoming audio is
enqueued for ASR -- they do NOT open or close the stream. This eliminates
the 50-200 ms WASAPI startup cost that was clipping the first phonemes of
each recording.

On `start()`, we also seed the ASR queue with the last PRE_ROLL_MS of the
rolling ring buffer so audio spoken slightly before the hotkey press is
included. This covers human reaction time (people often start speaking a
beat before the key registers).

WASAPI streams silently die across Windows sleep / hibernate / fast user
switching: the InputStream object stays "open" but no callbacks ever fire
again. A watchdog thread detects that (callbacks should fire continuously
even for silence) and rebuilds the stream automatically so the user never
has to relaunch the app after waking the PC.
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"
RECENT_SECONDS = 0.5          # ring buffer size (for overlay + pre-roll source)
PRE_ROLL_MS = 250             # prepended to each recording
SILENCE_GRACE_S = 4.0         # callbacks gone this long -> stream is dead
WATCHDOG_INTERVAL_S = 2.0     # how often the watchdog polls when stream is healthy
RESTART_VERIFY_S = 1.0        # after reopen, wait this long for a callback to prove it
RESTART_BACKOFF_MAX_S = 4.0   # cap retry spacing low: a USB mic re-enumerating
                              # after wake is ready within seconds, so a long
                              # backoff just leaves the mic dead far longer than
                              # the device actually needs.
REINIT_EVERY_N_ATTEMPTS = 5   # how often a silence-retry does the heavy global
                              # PortAudio re-init vs. just reopening the stream.
RECLAIM_CHECK_INTERVAL_S = 5  # while stranded on a non-preferred mic, how often
                              # to re-enumerate and look for the preferred one.

# Virtual / pass-through capture endpoints that Windows may momentarily make
# the default during a wake/boot storm. They deliver callbacks (so they look
# "alive") but are not the user's real mic, so we never auto-select them: we
# wait for a real device instead. The user's preferred mic, once locked, is
# always allowed even if its name matched here.
_VIRTUAL_DEVICE_HINTS = ("nvidia broadcast", "sound mapper",
                         "primary sound capture")

# Sentinel: preferred mic is set but currently absent -> do NOT open a
# fallback, wait for it to come back.
_DEVICE_ABSENT = object()
DEAD_DEVICE_S = 12.0          # callbacks flowing but bit-silent this long means
                              # we're bound to the wrong/dead endpoint (a fallback
                              # picked during a wake/hotplug storm); real mics
                              # always have a noise floor, so rebind to the default.
HOTPLUG_SETTLE_S = 1.5        # after a hotplug event, let Windows finish promoting
                              # the new default before we rebind to it.
SIGNAL_FLOOR = 1e-6           # |sample| above this counts as real audio signal

# ---------- Win32 device-change notification ----------
# WM_DEVICECHANGE fires on USB hotplug. We use it to force a stream rebind
# whenever an audio device arrives or leaves -- otherwise our InputStream
# stays bound to whichever device was default at open time, and a user who
# unplugs their preferred mic and then plugs it back in keeps capturing
# from the fallback device that became default during the unplug window.
_WM_DEVICECHANGE = 0x0219
_DBT_DEVICEARRIVAL = 0x8000
_DBT_DEVICEREMOVECOMPLETE = 0x8004
_HWND_MESSAGE = wintypes.HWND(-3)
_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

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


class _DeviceChangeNotifier:
    """Hidden message-only window that calls on_change for hotplug events."""

    def __init__(self, on_change) -> None:
        self._on_change = on_change
        self._wndproc_ref = _WNDPROC(self._wndproc)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="audio-device-notifier"
        )

    def start(self) -> None:
        self._thread.start()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_DEVICECHANGE and wparam in (
            _DBT_DEVICEARRIVAL, _DBT_DEVICEREMOVECOMPLETE,
        ):
            try:
                self._on_change()
            except Exception:
                log.exception("device-change handler raised")
            return 1
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        cls = _WNDCLASS()
        cls.lpfnWndProc = self._wndproc_ref
        cls.hInstance = _kernel32.GetModuleHandleW(None)
        cls.lpszClassName = "STTAudioDeviceNotifier"
        if not _user32.RegisterClassW(ctypes.byref(cls)):
            log.error("audio device RegisterClassW failed: %d",
                      _kernel32.GetLastError())
            return
        hwnd = _user32.CreateWindowExW(
            0, cls.lpszClassName, "stt-audio-device-notifier", 0,
            0, 0, 0, 0, _HWND_MESSAGE, 0, cls.hInstance, 0,
        )
        if not hwnd:
            log.error("audio device CreateWindowExW failed: %d",
                      _kernel32.GetLastError())
            return
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))


class MicStream:
    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        pre_roll_ms: int = PRE_ROLL_MS,
    ) -> None:
        self.sample_rate = sample_rate
        self._pre_roll_samples = int(sample_rate * pre_roll_ms / 1000)

        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: sd.InputStream | None = None
        self.current_level: float = 0.0
        self._level_smooth = 0.35

        self._recent = np.zeros(int(sample_rate * RECENT_SECONDS), dtype=np.float32)
        self._recent_lock = threading.Lock()

        self._recording = False
        self._recording_lock = threading.Lock()

        self._stream_lock = threading.Lock()
        self._closed = threading.Event()
        self._last_callback_at = time.monotonic()
        self._last_signal_at = time.monotonic()
        self._last_dead_rebind_at = 0.0
        self._last_reclaim_check_at = 0.0
        self._bound_device_name = "?"
        self._preferred_device_name: str | None = None
        self._waiting_for_pref_logged = False
        self._rebind_requested = False

        self._open_stream()
        if self._stream is not None:
            log.info("mic stream opened @ %d Hz (device: %s)",
                     self.sample_rate, self._bound_device_name)

        self._watchdog_thread = threading.Thread(
            target=self._watchdog, daemon=True, name="audio-watchdog"
        )
        self._watchdog_thread.start()

        self._device_notifier = _DeviceChangeNotifier(
            on_change=self._on_device_change
        )
        self._device_notifier.start()

    # --------------------------------------------------------------- stream
    def _open_stream(self) -> None:
        device = self._resolve_device()
        if device is _DEVICE_ABSENT:
            # Hard pin: the preferred mic isn't here right now. Do NOT bind a
            # fallback -- that's how we used to get stranded on NVIDIA
            # Broadcast. Leave the stream closed; the watchdog keeps retrying
            # (re-enumerating) until the preferred mic returns.
            self._stream = None
            if not self._waiting_for_pref_logged:
                log.warning("preferred mic '%s' absent; waiting for it "
                            "(not binding any fallback)",
                            self._preferred_device_name)
                self._waiting_for_pref_logged = True
            return
        self._stream = sd.InputStream(
            device=device,
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=self._callback,
        )
        self._stream.start()
        self._waiting_for_pref_logged = False
        try:
            info = (sd.query_devices(device) if device is not None
                    else sd.query_devices(kind="input"))
            self._bound_device_name = info["name"]
            if self._preferred_device_name is None:
                # Lock onto the mic we started with so a later hotplug can't
                # strand us on a virtual/fallback endpoint.
                self._preferred_device_name = self._bound_device_name
        except Exception:
            self._bound_device_name = "?"

    @staticmethod
    def _is_virtual(name: str) -> bool:
        low = name.lower()
        return any(hint in low for hint in _VIRTUAL_DEVICE_HINTS)

    def _resolve_device(self):
        """Pick the input device to open.

        - Preferred mic known: return its index if present, else _DEVICE_ABSENT
          (caller must wait, never bind a fallback).
        - No preferred yet (first open): the first real (non-virtual) input
          device, preferring the system default; _DEVICE_ABSENT if only
          virtual devices exist, so we never lock onto NVIDIA Broadcast."""
        try:
            devices = sd.query_devices()
        except Exception:
            return _DEVICE_ABSENT if self._preferred_device_name else None

        pref = self._preferred_device_name
        if pref:
            for idx, dev in enumerate(devices):
                if (dev.get("max_input_channels", 0) > 0
                        and dev.get("name") == pref):
                    return idx
            return _DEVICE_ABSENT

        # First open: choose the default input if it's a real device,
        # otherwise the first real input device. Never auto-pick a virtual one.
        try:
            default_name = sd.query_devices(kind="input")["name"]
        except Exception:
            default_name = None
        if default_name and not self._is_virtual(default_name):
            return None  # None == let PortAudio use the system default
        for idx, dev in enumerate(devices):
            if (dev.get("max_input_channels", 0) > 0
                    and not self._is_virtual(dev.get("name", ""))):
                return idx
        return _DEVICE_ABSENT

    def _callback(self, indata, frames, time_info, status) -> None:
        now = time.monotonic()
        if now - self._last_callback_at > 1.0:
            # Callback flow was interrupted (sleep/resume, restart, glitch).
            # Start a fresh signal window so the dead-device check measures
            # bit-silence only over continuous callback flow -- otherwise the
            # gap across a multi-hour sleep reads as hours of "silence" and
            # trips a spurious rebind the instant the mic wakes back up.
            self._last_signal_at = now
        self._last_callback_at = now
        data = indata.copy().reshape(-1)

        # Track when we last saw real signal. A working mic always carries a
        # noise floor; a wrong/dead endpoint returns bit-exact silence. The
        # watchdog uses this to detect "callbacks flowing but no audio".
        if data.size and float(np.abs(data).max()) > SIGNAL_FLOOR:
            self._last_signal_at = now

        # Always-on: rolling ring buffer (overlay + pre-roll source)
        with self._recent_lock:
            n = data.size
            if n >= self._recent.size:
                self._recent[:] = data[-self._recent.size:]
            else:
                self._recent[:-n] = self._recent[n:]
                self._recent[-n:] = data

        # Always-on: smoothed log-scale VU level
        rms = float(np.sqrt(np.mean(data * data) + 1e-12))
        norm = max(0.0, min(1.0, (np.log10(rms + 1e-6) + 4.0) / 3.5))
        self.current_level = (
            self._level_smooth * norm
            + (1.0 - self._level_smooth) * self.current_level
        )

        # Only enqueue for ASR when recording
        if self._recording:
            self._q.put(data)

    # --------------------------------------------------------- session API
    def start(self) -> None:
        """Begin a recording session. Seeds the queue with pre-roll audio."""
        with self._recording_lock:
            if self._recording:
                return
            # Clear any leftovers from prior sessions (shouldn't exist, but be safe)
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            # Seed with the last PRE_ROLL_MS of the ring buffer
            if self._pre_roll_samples > 0:
                with self._recent_lock:
                    seed = self._recent[-self._pre_roll_samples:].copy()
                self._q.put(seed)
            self._recording = True

    def stop(self) -> None:
        """End the recording session. The PortAudio stream stays open."""
        with self._recording_lock:
            self._recording = False
            self.current_level = 0.0
            # Zero the ring buffer so the tail of this session can't bleed into
            # the pre-roll seed of the next one if the user presses again fast.
            with self._recent_lock:
                self._recent[:] = 0.0

    def close(self) -> None:
        """Final teardown. Call on app exit."""
        self._closed.set()
        with self._recording_lock:
            self._recording = False
        with self._stream_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception as e:
                    log.error("mic close failed: %s", e)
                finally:
                    self._stream = None

    # --------------------------------------------------------- rebind API
    def request_rebind(self, reason: str) -> None:
        """Ask the watchdog to restart the stream on its next tick (<=2s).

        This is an explicit flag rather than the old trick of zeroing
        _last_callback_at: if callbacks are still flowing (wrong/dead-but-
        chatty device), the very next callback would overwrite the zeroed
        timestamp within ~30ms and the watchdog would never notice."""
        log.warning("mic rebind requested (%s)", reason)
        self._rebind_requested = True

    # --------------------------------------------------------- hotplug
    def _on_device_change(self) -> None:
        """Rebind the mic shortly after a USB hotplug.

        We wait HOTPLUG_SETTLE_S first: a device-arrival event fires before
        Windows promotes the device to the default endpoint, so rebinding
        immediately would re-open the old fallback."""
        log.info("audio device hotplug; rebinding after %.1fs settle",
                 HOTPLUG_SETTLE_S)

        def _arm() -> None:
            if not self._closed.wait(HOTPLUG_SETTLE_S):
                self._rebind_requested = True

        threading.Thread(
            target=_arm, daemon=True, name="audio-hotplug-rebind"
        ).start()

    # --------------------------------------------------------- watchdog
    def _watchdog(self) -> None:
        """Detect stream death (callbacks stopped) and recover.

        Verifies each restart by waiting RESTART_VERIFY_S and checking that
        _last_callback_at advanced -- it only advances from real callbacks,
        not from anything the restart does. Backs off exponentially up to
        RESTART_BACKOFF_MAX_S so a sustained-dead device doesn't generate a
        4-second restart storm in the log. Outer try/except keeps the thread
        alive across any unexpected failure."""
        next_wait = WATCHDOG_INTERVAL_S
        consecutive_failed_restarts = 0
        reported_dead = False
        while not self._closed.wait(next_wait):
            try:
                if self._rebind_requested:
                    self._rebind_requested = False
                    self._restart_stream(reinit_portaudio=True)
                    log.info("explicit rebind done (device: %s)",
                             self._bound_device_name)
                    self._last_signal_at = time.monotonic()
                    next_wait = WATCHDOG_INTERVAL_S
                    continue
                gap = time.monotonic() - self._last_callback_at
                if gap <= SILENCE_GRACE_S:
                    if reported_dead:
                        log.info("mic stream restored (callbacks resumed) "
                                 "(device: %s)", self._bound_device_name)
                        reported_dead = False
                        self._last_signal_at = time.monotonic()
                    consecutive_failed_restarts = 0
                    next_wait = WATCHDOG_INTERVAL_S
                    if not self._reclaim_preferred_device():
                        self._check_dead_device()
                    continue
                if not reported_dead:
                    log.warning("mic stream silent for %.1fs -- restarting", gap)
                    reported_dead = True
                restart_at = time.monotonic()
                # Heavy PortAudio re-init only every Nth attempt: a constant
                # global reset prevents the subsystem from settling post-wake.
                # But while we're waiting for an absent preferred mic (no
                # stream open), always re-init so we actually see it come back
                # -- there's no live stream for the reset to disturb.
                waiting = self._stream is None and self._preferred_device_name
                reinit = (bool(waiting)
                          or consecutive_failed_restarts
                          % REINIT_EVERY_N_ATTEMPTS == 0)
                self._restart_stream(reinit_portaudio=reinit)
                if self._closed.wait(RESTART_VERIFY_S):
                    return
                if self._last_callback_at > restart_at:
                    log.info("mic stream restored after %d attempt(s) (device: %s)",
                             consecutive_failed_restarts + 1,
                             self._bound_device_name)
                    consecutive_failed_restarts = 0
                    reported_dead = False
                    next_wait = WATCHDOG_INTERVAL_S
                else:
                    consecutive_failed_restarts += 1
                    next_wait = min(
                        WATCHDOG_INTERVAL_S
                        * (2 ** min(consecutive_failed_restarts, 6)),
                        RESTART_BACKOFF_MAX_S,
                    )
            except Exception:
                log.exception("audio watchdog tick failed")
                next_wait = WATCHDOG_INTERVAL_S

    def _reclaim_preferred_device(self) -> bool:
        """If we're bound to a fallback but the preferred mic is back, retake
        it. Returns True if a rebind was issued.

        Cold boot / wake is the killer case: the Yeti is briefly absent while
        the subsystem settles, so a restart falls back to the system default
        -- which can be a virtual device like NVIDIA Broadcast that delivers
        callbacks (so the liveness and dead-device checks are both satisfied)
        and we get stranded there.

        Crucially we must RE-ENUMERATE before checking presence:
        sd.query_devices() reads PortAudio's cached list, frozen at the last
        init. On the healthy path we never re-init, so a stale list would hide
        the Yeti's return and we'd stay stuck forever -- which is exactly the
        bug this had. Re-init is heavy, so do it at most every
        RECLAIM_CHECK_INTERVAL_S, and only while actually on the wrong mic."""
        pref = self._preferred_device_name
        if not pref or self._bound_device_name == pref:
            return False
        now = time.monotonic()
        if now - self._last_reclaim_check_at < RECLAIM_CHECK_INTERVAL_S:
            return False
        self._last_reclaim_check_at = now
        # Refresh PortAudio's device list so a mic that re-enumerated after
        # wake is actually visible.
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            log.exception("device re-enumerate failed during reclaim")
            return False
        try:
            devices = sd.query_devices()
        except Exception:
            return False
        present = any(
            d.get("max_input_channels", 0) > 0 and d.get("name") == pref
            for d in devices
        )
        if not present:
            return False
        log.info(
            "preferred mic '%s' is back (currently on '%s') -- reclaiming",
            pref, self._bound_device_name,
        )
        # List is already fresh from the re-enumerate above; don't redo it.
        self._restart_stream(reinit_portaudio=False)
        log.info("rebound to device: %s", self._bound_device_name)
        self._last_signal_at = time.monotonic()
        return True

    def _check_dead_device(self) -> None:
        """Rebind if callbacks are flowing but carry only digital silence.

        After a wake/hotplug storm the stream can bind to a fallback endpoint
        that delivers callbacks full of zeros -- the watchdog's liveness check
        passes, but the user sees a flat waveform and gets no transcript. A
        real mic always has a noise floor, so prolonged bit-silence means the
        wrong device. Rate-limited so a genuinely muted mic doesn't loop."""
        now = time.monotonic()
        if now - self._last_signal_at <= DEAD_DEVICE_S:
            return
        if now - self._last_dead_rebind_at <= DEAD_DEVICE_S:
            return
        log.warning(
            "mic device '%s' digitally silent for %.0fs -- likely wrong "
            "endpoint; rebinding to current default",
            self._bound_device_name, now - self._last_signal_at,
        )
        self._last_dead_rebind_at = now
        self._restart_stream()
        log.info("rebound to device: %s", self._bound_device_name)
        # Fresh window so we re-evaluate the new binding rather than
        # immediately tripping again on the same timestamp.
        self._last_signal_at = time.monotonic()

    def _restart_stream(self, reinit_portaudio: bool = True) -> None:
        with self._stream_lock:
            if self._closed.is_set():
                return
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    log.exception("error closing dead mic stream")
                self._stream = None
            # Optionally force PortAudio to re-enumerate devices. Its device
            # list is cached at library init, so a USB unplug/replug or a
            # default-endpoint switch needs this to be seen. But it is a heavy
            # GLOBAL reset, and hammering it every few seconds while the audio
            # subsystem is still settling after wake/boot keeps knocking the
            # device back down before a stream can stabilise -- which is what
            # caused a 5-minute, 55-attempt dead window after sleep. So the
            # caller throttles it: re-init occasionally, just reopen otherwise.
            if reinit_portaudio:
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    log.exception("PortAudio re-init failed; continuing anyway")
            try:
                self._open_stream()
                self._last_signal_at = time.monotonic()  # fresh window per binding
            except Exception:
                log.exception("mic reopen failed; will retry on next tick")

    # --------------------------------------------------------- consumer API
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
