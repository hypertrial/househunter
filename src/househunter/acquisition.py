from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .config import RuntimePaths
from .cost_of_living import download_bea_rpp
from .errors import HouseHunterError

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]
Download = Callable[..., Path]


def download_optional_bea(
    paths: RuntimePaths,
    *,
    download: Download = download_bea_rpp,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> tuple[Path | None, str | None]:
    """Acquire BEA when possible without making it a core-snapshot dependency."""
    try:
        return download(paths, progress=progress, cancelled=cancelled), None
    except InterruptedError:
        raise
    except (HouseHunterError, OSError) as exc:
        warning = f"Optional BEA RPP source unavailable: {exc}"
        if progress:
            progress(100, warning)
        return None, warning
