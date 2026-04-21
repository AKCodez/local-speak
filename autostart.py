"""Windows autostart-on-login via the HKCU Run registry key.

Uses stdlib `winreg` only -- no pywin32 dependency. Per-user scope, so no
UAC prompt is ever needed.
"""
from __future__ import annotations

import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "STTDictation"


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _command() -> str:
    """The command written to the Run key. Uses run.vbs for a fully silent launch."""
    vbs = _project_root() / "run.vbs"
    return f'wscript.exe "{vbs}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable() -> None:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, _command())


def disable() -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.DeleteValue(k, VALUE_NAME)
    except FileNotFoundError:
        pass


def set_enabled(value: bool) -> None:
    if value:
        enable()
    else:
        disable()
