from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .errors import HouseHunterError

OWNERSHIP_MARKER = ".househunter-mountain-owned"


def lexical_path(path: Path) -> Path:
    """Return an absolute path without resolving symlinks."""
    return Path(os.path.abspath(path.expanduser()))


def ensure_safe_directory(path: Path) -> Path:
    """Create a directory while refusing every symlinked path component."""
    target = lexical_path(path)
    for component in (target, *target.parents):
        if component.is_symlink():
            raise HouseHunterError(
                f"Mountain managed path contains a symlink component: {component}"
            )
    target.mkdir(parents=True, exist_ok=True)
    for component in (target, *target.parents):
        if component.is_symlink():
            raise HouseHunterError(
                f"Mountain managed path contains a symlink component: {component}"
            )
    if not target.is_dir():
        raise HouseHunterError(f"Mountain managed path is not a directory: {target}")
    return target


def require_owned_child(child: Path, parent: Path, *, name_pattern: str) -> Path:
    """Validate a direct pipeline-owned child using lexical containment."""
    safe_parent = ensure_safe_directory(parent)
    safe_child = lexical_path(child)
    marker = safe_child / OWNERSHIP_MARKER
    if (
        safe_child.parent != safe_parent
        or re.fullmatch(name_pattern, safe_child.name) is None
        or safe_child.is_symlink()
        or not safe_child.is_dir()
        or not marker.is_file()
        or marker.is_symlink()
    ):
        raise HouseHunterError(
            f"Refusing to clean unowned or use unsafe Mountain directory: {safe_child}"
        )
    return safe_child


def ensure_owned_child(
    child: Path,
    parent: Path,
    *,
    name_pattern: str,
    marker_value: str,
) -> Path:
    """Create a direct owned child, never adopting an existing unmarked directory."""
    safe_parent = ensure_safe_directory(parent)
    safe_child = lexical_path(child)
    if safe_child.parent != safe_parent or re.fullmatch(name_pattern, safe_child.name) is None:
        raise HouseHunterError(f"Mountain managed child path is invalid: {safe_child}")
    if safe_child.exists() or safe_child.is_symlink():
        return require_owned_child(safe_child, safe_parent, name_pattern=name_pattern)
    try:
        safe_child.mkdir()
        marker = safe_child / OWNERSHIP_MARKER
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w") as handle:
            handle.write(marker_value)
    except BaseException:
        if safe_child.is_dir() and not safe_child.is_symlink() and not any(safe_child.iterdir()):
            safe_child.rmdir()
        raise
    return require_owned_child(safe_child, safe_parent, name_pattern=name_pattern)


def remove_owned_child(child: Path, parent: Path, *, name_pattern: str) -> None:
    safe_child = require_owned_child(child, parent, name_pattern=name_pattern)
    shutil.rmtree(safe_child)
