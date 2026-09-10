from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from .errors import HouseHunterError


@contextmanager
def exclusive_lock(path: Path):  # type: ignore[no-untyped-def]
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: TextIO = path.open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HouseHunterError("Another HouseHunter process is modifying data") from exc
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
