"""The coordinator's own log: everything its terminal shows, plus warnings and tracebacks.

Saved as ``<state_dir>/supervisor/<identity>/coordinator.log`` and rotated at 5 MB (five older
files are kept). Closing or detaching the terminal never loses it; `coordinator logs` shows and
follows it, `coordinator open` opens it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from delivery.journal import ensure_private_dir

LOG_NAME = "coordinator.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5

log = logging.getLogger("delivery")
console_log = logging.getLogger("delivery.console")


def log_path(state_dir: Path, identity_key: str) -> Path:
    return state_dir / "supervisor" / identity_key / LOG_NAME


class _Formatter(logging.Formatter):
    """Terminal text as it was shown; other records with time and level. A dated line starts
    each new day, because the terminal blocks only show the time."""

    def __init__(self) -> None:
        super().__init__()
        self._day = ""

    def format(self, record: logging.LogRecord) -> str:
        # The rotating handler formats each record twice (once to check the size).
        done = getattr(record, "_coordinator_text", None)
        if isinstance(done, str):
            return done
        when = datetime.fromtimestamp(record.created)
        day = when.strftime("%A %d %B %Y")
        prefix = ""
        if day != self._day:
            self._day = day
            prefix = f"\n----- {day} -----\n"
        if getattr(record, "console", False):
            text = record.getMessage()
        else:
            text = f"[{when:%H:%M:%S}] {record.levelname} {record.getMessage()}"
            if record.exc_info:
                text += "\n" + self.formatException(record.exc_info)
        record._coordinator_text = prefix + text
        return prefix + text


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return stream


def attach(path: Path, verbose: bool = False) -> logging.Handler:
    """Send the coordinator's terminal output and its log records to ``path``."""
    ensure_private_dir(path.parent)
    handler = _PrivateRotatingHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    handler.setFormatter(_Formatter())
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    return handler


def detach(handler: logging.Handler) -> None:
    log.removeHandler(handler)
    handler.close()


def emitter(write: Callable[[str], None] | None = None) -> Callable[[str], None]:
    """Show a line or block in the terminal (when there still is one) and keep it in the log."""

    def emit(text: str) -> None:
        # OSError/ValueError: the terminal is gone (window closed); the log still has it.
        with contextlib.suppress(OSError, ValueError):
            (write or _print)(text)
        console_log.info(text, extra={"console": True})

    return emit


def _print(text: str) -> None:
    print(text, flush=True)


def tail(path: Path, lines: int = 80) -> list[str]:
    if not path.is_file():
        return []
    with path.open(errors="replace") as fh:
        return fh.read().splitlines()[-lines:]


def follow(path: Path, poll: float = 1.0, stop: Callable[[], bool] = lambda: False) -> Iterator[str]:
    """New lines as they are written, across rotation, until ``stop()``."""
    fh = None
    inode = None
    at_end = True  # the caller has shown what was there; after a rotation, read the new file
    while not stop():
        if fh is None:
            try:
                fh = path.open(errors="replace")
            except OSError:
                at_end = False
                time.sleep(poll)
                continue
            inode = os.fstat(fh.fileno()).st_ino
            if at_end:
                fh.seek(0, os.SEEK_END)
        line = fh.readline()
        if line:
            yield line.rstrip("\n")
            continue
        try:
            rotated = os.stat(path).st_ino != inode
        except OSError:
            rotated = True
        if rotated:
            fh.close()
            fh, at_end = None, False
            continue
        time.sleep(poll)
    if fh is not None:
        fh.close()
