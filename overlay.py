"""State-of-the-art dictation indicator overlay.

A small rounded bubble pinned bottom-centre on the primary monitor. Shows a
pulsing record dot, status text, and a live mirrored waveform driven by the
most recent ~0.5 s of mic audio.

Uses Pillow to composite, then a magenta colour-key on the Tk root for the
"transparent" region outside the bubble. Windows Win32 extended styles keep
the overlay from stealing keyboard focus.
"""
from __future__ import annotations

import ctypes
import queue
import tkinter as tk
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

# ---------- geometry ----------
W, H = 340, 68
RADIUS = 22
PAD = 14
DOT_X = 26
DOT_R = 5
TEXT_X = DOT_X + DOT_R + 12
BAR_COUNT = 32
BAR_W = 3
BAR_GAP = 3
BAR_MAX_HALF = (H // 2) - PAD

# ---------- colours (RGB) ----------
KEY = (255, 0, 255)            # magenta key color keyed to transparent
BG = (10, 22, 40)              # deep navy bubble body
BG_EDGE = (20, 42, 70)         # subtle lighter ring
BORDER = (56, 189, 248)        # cyan edge
GLOW = (99, 179, 237)          # inner glow
TEXT = (226, 240, 255)
TEXT_DIM = (160, 180, 210)
DOT_ON = (56, 189, 248)
DOT_OFF = (90, 110, 140)
BAR_ON = (56, 189, 248)
BAR_MID = (96, 165, 250)
BAR_OFF = (50, 70, 100)

# ---------- window placement ----------
Y_OFFSET = 100  # pixels from bottom of primary monitor

# ---------- Win32 ExStyle ----------
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("segoeuisb.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


class Overlay:
    def __init__(
        self,
        level_provider: Callable[[], float] | None = None,
        samples_provider: Callable[[], np.ndarray] | None = None,
    ) -> None:
        self._level_provider = level_provider or (lambda: 0.0)
        self._samples_provider = samples_provider or (lambda: np.zeros(8000, dtype=np.float32))
        self._status = "Listening"
        self._active = True
        self._ui_q: queue.Queue = queue.Queue()
        self._on_close: Callable[[], None] | None = None
        self._visible = False
        self._frame = 0
        self._font = _load_font(14)

        # Pre-render the static background (bubble + border + glow + dot halo).
        # We only recompose the dynamic layer (dot pulse + text + bars) each frame.
        self._bg_image = self._make_background()

        self.root = tk.Tk()
        self.root.title("STT")
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.configure(bg=self._rgb_hex(KEY))
        self.root.wm_attributes("-transparentcolor", self._rgb_hex(KEY))

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - W) // 2
        y = sh - H - Y_OFFSET
        self.root.geometry(f"{W}x{H}+{x}+{y}")

        self.label = tk.Label(
            self.root, bg=self._rgb_hex(KEY), bd=0, highlightthickness=0
        )
        self.label.pack()

        self.root.withdraw()
        self.root.update_idletasks()
        self._apply_no_activate_style()

        # One-shot initial compose so self._photo is valid before show
        self._compose_and_set()
        self.root.after(33, self._tick)

    # ==================== public API ====================
    def show(self) -> None:
        if not self._visible:
            self.root.deiconify()
            self._visible = True
            self._apply_no_activate_style()

    def hide(self) -> None:
        if self._visible:
            self.root.withdraw()
            self._visible = False

    def set_status(self, text: str, active: bool = True) -> None:
        self._status = text
        self._active = active

    def call_on_ui(self, fn: Callable, *args) -> None:
        self._ui_q.put((fn, args))

    def set_close_callback(self, cb: Callable[[], None]) -> None:
        self._on_close = cb

    def mainloop(self) -> None:
        try:
            self.root.mainloop()
        finally:
            if self._on_close:
                self._on_close()

    def request_quit(self) -> None:
        self.root.after(0, self.root.quit)

    # ==================== internals ====================
    @staticmethod
    def _rgb_hex(rgb: tuple[int, int, int]) -> str:
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    def _apply_no_activate_style(self) -> None:
        try:
            user32 = ctypes.windll.user32
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_long,
            ]
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, _GWL_EXSTYLE, style | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
            )
        except Exception as e:
            print(f"[overlay] no-activate style failed: {e}")

    # ---------------- rendering ----------------
    def _make_background(self) -> Image.Image:
        """Static layer: bubble body + border + inner glow. Pre-rendered once."""
        # Work in RGBA for nice blur / blending, then key-in at the very end.
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(base)

        # Glow halo behind the bubble (very soft)
        halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(halo).rounded_rectangle(
            (3, 3, W - 4, H - 4), radius=RADIUS, fill=(*GLOW, 55)
        )
        halo = halo.filter(ImageFilter.GaussianBlur(10))
        base = Image.alpha_composite(base, halo)

        # Body
        bd = ImageDraw.Draw(base)
        bd.rounded_rectangle(
            (0, 0, W - 1, H - 1), radius=RADIUS, fill=(*BG, 235), outline=None
        )
        # Slightly lighter upper highlight for depth
        highlight = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(highlight).rounded_rectangle(
            (1, 1, W - 2, H // 2), radius=RADIUS, fill=(*BG_EDGE, 60)
        )
        highlight = highlight.filter(ImageFilter.GaussianBlur(4))
        base = Image.alpha_composite(base, highlight)

        # Border (cyan hairline)
        bd = ImageDraw.Draw(base)
        bd.rounded_rectangle(
            (0, 0, W - 1, H - 1), radius=RADIUS, outline=(*BORDER, 150), width=1
        )

        return base

    def _compose_frame(self) -> Image.Image:
        """Full composite: static background + dynamic dot/text/waveform."""
        img = self._bg_image.copy()

        # Record dot with pulsing halo
        self._draw_dot(img)

        # Status text
        draw = ImageDraw.Draw(img)
        text_color = TEXT if self._active else TEXT_DIM
        draw.text((TEXT_X, H // 2 - 9), self._status, fill=(*text_color, 255), font=self._font)

        # Waveform
        self._draw_waveform(img)

        return img

    def _draw_dot(self, img: Image.Image) -> None:
        cx, cy = DOT_X, H // 2
        # Halo — breathing while active
        if self._active:
            t = self._frame / 30.0  # ~1 Hz pulse given 30 FPS
            pulse = 0.5 + 0.5 * np.sin(t * 2 * np.pi * 1.2)
            halo_r = int(DOT_R + 6 + pulse * 4)
            halo_alpha = int(90 + pulse * 80)
            halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(halo).ellipse(
                (cx - halo_r, cy - halo_r, cx + halo_r, cy + halo_r),
                fill=(*DOT_ON, halo_alpha),
            )
            halo = halo.filter(ImageFilter.GaussianBlur(5))
            img.alpha_composite(halo)

        dot = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(dot).ellipse(
            (cx - DOT_R, cy - DOT_R, cx + DOT_R, cy + DOT_R),
            fill=(*(DOT_ON if self._active else DOT_OFF), 255),
        )
        img.alpha_composite(dot)

    def _draw_waveform(self, img: Image.Image) -> None:
        # Layout
        measure = ImageDraw.Draw(img).textlength(self._status, font=self._font)
        bars_left = int(TEXT_X + max(measure + 14, 60))
        bars_right = W - PAD
        total_w = BAR_COUNT * (BAR_W + BAR_GAP) - BAR_GAP
        if bars_left + total_w > bars_right:
            bars_left = bars_right - total_w  # right-align if squeezed

        samples = self._samples_provider()
        heights = self._compute_bar_heights(samples)

        cy = H // 2
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for i, h in enumerate(heights):
            x = bars_left + i * (BAR_W + BAR_GAP)
            half = max(2, int(h * BAR_MAX_HALF))
            # colour interpolation: brighter bars use BAR_ON, shorter use BAR_MID / BAR_OFF
            if h < 0.05:
                color = BAR_OFF
            elif h < 0.4:
                color = BAR_MID
            else:
                color = BAR_ON
            alpha = 230 if self._active else 120
            d.rounded_rectangle(
                (x, cy - half, x + BAR_W, cy + half),
                radius=BAR_W // 2,
                fill=(*color, alpha),
            )
        img.alpha_composite(layer)

    @staticmethod
    def _compute_bar_heights(samples: np.ndarray) -> np.ndarray:
        n = samples.size
        if n == 0:
            return np.zeros(BAR_COUNT, dtype=np.float32)
        chunk = max(1, n // BAR_COUNT)
        # Trim to exact multiple so reshape works
        usable = chunk * BAR_COUNT
        s = samples[-usable:]
        frames = s.reshape(BAR_COUNT, chunk)
        rms = np.sqrt((frames * frames).mean(axis=1) + 1e-12)
        # Log-scale to emphasise voice range (quiet -4 .. loud -0.5)
        norm = np.clip((np.log10(rms + 1e-6) + 4.0) / 3.5, 0.0, 1.0)
        # Subtle shaping so quiet chunks still show a nub
        return 0.08 + 0.92 * norm

    def _compose_and_set(self) -> None:
        img = self._compose_frame()
        flat = self._flatten_to_keycolor(img)
        self._photo = ImageTk.PhotoImage(flat)
        self.label.configure(image=self._photo)

    @staticmethod
    def _flatten_to_keycolor(rgba: Image.Image) -> Image.Image:
        """RGBA -> RGB, mapping fully-transparent pixels to the magenta key.

        Semi-transparent AA pixels are flattened against the bubble body colour
        so the rounded edges don't leave a magenta fringe.
        """
        arr = np.array(rgba, dtype=np.uint8)
        rgb = arr[..., :3].astype(np.float32)
        alpha = arr[..., 3:4].astype(np.float32) / 255.0
        # Flatten against BG (bubble interior) for AA pixels
        flat = (rgb * alpha + np.array(BG, dtype=np.float32) * (1.0 - alpha)).astype(np.uint8)
        # Fully transparent -> key (magenta). Threshold >0 keeps AA pixels visible.
        mask = arr[..., 3] == 0
        flat[mask] = KEY
        return Image.fromarray(flat, "RGB")

    # ---------------- event loop ----------------
    def _tick(self) -> None:
        # Drain UI queue
        while True:
            try:
                fn, args = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as e:
                print(f"[overlay] UI call error: {e}")

        if self._visible:
            self._frame += 1
            self._compose_and_set()

        self.root.after(33, self._tick)
