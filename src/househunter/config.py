from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import HouseHunterError


@dataclass(frozen=True)
class RuntimePaths:
    data: Path
    cache: Path
    raw: Path
    processed: Path
    builds: Path
    current: Path
    source_manifest: Path
    job_lock: Path

    @classmethod
    def from_root(cls, root: Path | None = None) -> RuntimePaths:
        configured = os.environ.get("HOUSEHUNTER_DATA_DIR")
        data = Path(configured) if configured else (root or Path.cwd()) / "data"
        data = data.expanduser().resolve()
        return cls(
            data=data,
            cache=data / "cache",
            raw=data / "raw",
            processed=data / "processed",
            builds=data / "builds",
            current=data / "current.json",
            source_manifest=data / "source_manifest.parquet",
            job_lock=data / ".mutating-job.lock",
        )

    def ensure(self) -> None:
        for directory in (self.data, self.cache, self.raw, self.processed, self.builds):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)


def default_config_path() -> Path:
    explicit = os.environ.get("HOUSEHUNTER_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    checkout_config = Path(__file__).resolve().parents[2] / "config" / "sources.yml"
    if checkout_config.is_file():
        return checkout_config
    packaged = Path(__file__).with_name("assets") / "sources.yml"
    if packaged.is_file():
        return packaged
    raise HouseHunterError("Cannot find config/sources.yml; set HOUSEHUNTER_CONFIG")


def load_config(path: Path | None = None) -> dict[str, Any]:
    source = path or default_config_path()
    try:
        parsed = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise HouseHunterError(f"Cannot read source configuration: {source}: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        raise HouseHunterError(f"Unsupported source configuration: {source}")
    return parsed


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: object) -> None:
    """Durably replace one JSON file without exposing a partial write."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
