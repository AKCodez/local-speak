# local-speak

**Fast, local, offline speech-to-text dictation for Windows.**
Hold a key, speak, release — the transcript is typed into whatever app has focus. No cloud, no API keys, no network calls after setup.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3118/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-blue.svg)]()
[![GPU: NVIDIA CUDA](https://img.shields.io/badge/GPU-NVIDIA%20CUDA%2012.8-76B900.svg)]()

---

## What it is

local-speak is a desktop dictation client powered by OpenAI's Whisper (large-v3-turbo), running entirely on your GPU via [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Hold **Right Ctrl** anywhere in Windows, speak, release — and the transcript appears in your focused app, whether that's Slack, VS Code, Notepad, your browser, or a game chat.

While recording, a translucent bubble shows a live mirrored waveform of your voice. When you release, the full clip is transcribed in one shot (usually <1 s on a modern NVIDIA GPU) and typed into the focused window.

End-to-end latency on an RTX 5090 for a 10 s clip: ~500 ms.

## Features

- **100% local.** Your audio never leaves your machine. No account, no internet after setup.
- **Hold-to-talk**, not always-listening. Zero privacy exposure outside your conscious use.
- **Real-time waveform overlay** — a 340×68 px translucent bubble pinned bottom-centre, driven by the last 0.5 s of live mic audio (32 mirrored bars, log-scaled RMS, pulsing record dot with Gaussian-blurred halo).
- **System-tray admin panel**: Pause, Start-with-Windows, Open log folder, Quit.
- **Silent autostart** — optional. Toggled from the tray. Writes a single HKCU registry Run value (no UAC prompt, per-user scope).
- **Two launchers**: `run.vbs` (silent, no console, no taskbar entry) and `run.bat` (debug, console visible).
- **Rotating log** at `%LOCALAPPDATA%\STT\stt.log` (1 MB × 3 files) for troubleshooting.
- **Thread-safe transcription** — a lock around the model serialises concurrent requests cleanly.

## Requirements

| | |
|---|---|
| OS | Windows 10 / 11 |
| Python | 3.11 |
| GPU | NVIDIA with CUDA 12.8 driver (572+ for Blackwell 50-series, 555+ for 30/40-series) |
| VRAM | ~2 GB free |
| Microphone | Anything Windows recognises |
| Disk | ~4 GB (PyTorch + Whisper model) |

CPU-only operation is not currently supported — the code assumes `device="cuda"` with `float16` compute.

## Quick start

```powershell
git clone https://github.com/AKCodez/local-speak.git
cd local-speak
setup.bat
```

Then double-click **`run.vbs`**. Tray icon appears when the model is loaded (~3 s).

First launch downloads the Whisper model (~800 MB). Subsequent launches are instant.

## Usage

1. Focus any text input — Notepad, a chat box, a code editor, a browser field.
2. **Hold Right Ctrl.** The waveform bubble fades in.
3. Speak.
4. **Release Right Ctrl.** Bubble flips to *Transcribing…*, then the transcript types itself into the focused window.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Hold **Right Ctrl** | Record |
| Release **Right Ctrl** | Transcribe + type |
| **Ctrl + Alt + Q** | Quit from anywhere |

## Tray menu

Right-click the tray icon:

| Menu item | What it does |
|---|---|
| **Start with Windows** | Toggles the `HKCU\…\Run\STTDictation` registry value. When on, the app launches silently on every login. |
| **Pause dictation** | Temporarily ignores Right Ctrl — useful while gaming or if the hotkey conflicts with something. |
| **Open log folder** | Opens `%LOCALAPPDATA%\STT\` in Explorer. |
| **Quit** | Graceful shutdown — stops the listener, mic, and tray; releases VRAM. |

## Architecture

```
Right Ctrl down             Right Ctrl up
        |                       |
        v                       v
 +--------------+        +---------------+
 | MicStream    |------->| drain buffer  |
 | (sounddevice)|        +-------+-------+
 +------+-------+                |
        |                        v
        v                +---------------+     +-----------------+
 +--------------+        | faster-whisper|---->| pynput Typist   |
 | Rolling 0.5s |        | large-v3-turbo|     | types into focus|
 | waveform     |        | fp16 on CUDA  |     +-----------------+
 +------+-------+        +---------------+
        |
        v
 +--------------+
 | Overlay      |
 | Pillow+Tk    |
 | 30 FPS bubble|
 +--------------+
```

Main thread owns the Tk mainloop for the overlay. Mic callbacks run on PortAudio's thread, the keyboard hook on pynput's thread, finalize/transcribe on a throwaway daemon thread, and the tray on its own pystray thread. A single lock inside `Dictation` serialises state transitions (active → finalizing → idle).

## Why faster-whisper large-v3-turbo

As of Q1 2026, top of the [Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard):

| Model | WER | RTFx | Streaming | Pick for this use |
|---|---|---|---|---|
| Canary-Qwen-2.5B | 5.63% | 418 | no (batch) | Too slow end-to-end; autoregressive decoder |
| Parakeet TDT 0.6B v3 | ~6.5% | ~2700 | yes | On the roadmap |
| **faster-whisper large-v3-turbo** | ~7% | ~200 | batch | Easiest install; plenty fast for hold-to-talk |
| faster-whisper large-v3 (full) | ~6.5% | ~120 | batch | Drop-in accuracy upgrade |

For hold-to-talk there's no streaming benefit — we transcribe the full clip after release. Turbo wins on installation simplicity (a single `pip install` gets CUDA kernels and model loader) and gives us ~500 ms end-to-end on a 5090.

## Troubleshooting

### "Windows protected your PC" on first launch
Unsigned Python code + a keyboard hook = SmartScreen warning. Click **More info** → **Run anyway**. The code is fully readable in this repo — audit before running if you're unsure.

### Tray icon doesn't appear
Launch `run.bat` instead of `run.vbs` so you can see the error:
- **`CUDA is not available`** — your driver is too old. Update from [NVIDIA](https://www.nvidia.com/en-us/drivers).
- **`ModuleNotFoundError`** — re-run `setup.bat`.
- **`PortAudioError` / no audio device** — Settings → Privacy & security → Microphone → allow desktop apps.

### Transcription is missing short words like "I"
Already mitigated with `beam_size=5` in `asr.py`. If it still happens on your voice, try `large-v3` (not turbo) — edit `main.py:Dictation.__init__`, replace `"large-v3-turbo"` with `"large-v3"`. ~2× slower but marginally more accurate.

### Double-spacing between segments
Already normalised in `asr.py` via `re.sub(r"\s+", " ", text).strip()`.

### Antivirus flags it as a keylogger
pynput uses `WH_KEYBOARD_LL` — a Windows low-level keyboard hook. That API is used by both dictation tools and keyloggers, so heuristics trip. The code only checks for Right Ctrl + Ctrl+Alt+Q; it never stores or transmits keystrokes. Audit `main.py` to confirm. Add a folder exclusion if needed.

### Right Ctrl conflicts with a shortcut I use
Edit `HOTKEY = keyboard.Key.ctrl_r` in `main.py`. Alternatives that don't cause combo conflicts:
- `keyboard.Key.menu` — the right-click / context-menu key
- `keyboard.Key.pause`
- `keyboard.Key.caps_lock` (toggle will still fire; needs extra suppression)

## Project layout

```
local-speak/
├── main.py           Entry: wires tray, overlay, listener, signal handlers
├── asr.py            faster-whisper wrapper, thread-safe
├── audio.py          Mic capture + rolling waveform ring buffer
├── overlay.py        Live-waveform bubble (Pillow composited, Tk displayed)
├── inject.py         Keystroke output via pynput
├── tray.py           pystray menu + generated icon
├── autostart.py      HKCU Run key read/write/delete
├── logutil.py        RotatingFileHandler setup
├── run.vbs           Silent launcher (no console)
├── run.bat           Debug launcher (console visible)
├── setup.bat         One-click install script
├── requirements.txt
├── LICENSE           MIT
└── README.md         (you are here)
```

## Roadmap

- [ ] Swap model to **Parakeet TDT 0.6B v3** (NeMo) for sub-200 ms end-to-end
- [ ] Silero-VAD auto-endpoint so you don't have to release the key
- [ ] Configurable hotkey via tray submenu + persistent settings file
- [ ] Clipboard-paste injection mode (for very long transcripts or keystroke-hostile apps)
- [ ] TensorRT encoder export for the lowest-latency path

## Contributing

Personal project, PRs welcome. Open an issue first for anything non-trivial so we can align on scope.

## License

[MIT](LICENSE) — do whatever you want, attribution appreciated, no warranty.

## Credits

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — CTranslate2-accelerated Whisper that makes local GPU ASR feel instant.
- [OpenAI Whisper](https://github.com/openai/whisper) — the underlying model.
- [pystray](https://github.com/moses-palmer/pystray) — cross-platform tray icons.
- [pynput](https://github.com/moses-palmer/pynput) — global keyboard hooks.
- [sounddevice](https://github.com/spatialaudio/python-sounddevice) — PortAudio bindings.
- [Pillow](https://python-pillow.org/) — overlay rendering and the tray icon, generated at runtime.
