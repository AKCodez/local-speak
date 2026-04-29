"""Rotating file logging for STT. Mirrors to console when one is attached."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "STT"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return log_dir() / "stt.log"


def configure(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Quiet third-party loggers that would otherwise leak per-clip metadata
    # (audio duration, model download URLs) into the log file.
    for name in ("faster_whisper", "httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Mirror to stdout only if a console is attached (i.e. run via run.bat,
    # not pythonw.exe which has no stdout).
    try:
        if sys.stdout and sys.stdout.isatty():
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            root.addHandler(sh)
    except Exception:
        pass
