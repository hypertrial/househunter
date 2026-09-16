from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import socket
import stat
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .errors import HouseHunterError

Cancelled = Callable[[], bool]

MAX_REDIRECTS = 5
CHUNK_SIZE = 1024 * 1024
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200_000
MAX_ARCHIVE_UNCOMPRESSED = 30_000_000_000
MAX_COMPRESSION_RATIO = 100
DENIED_DATA_CLASSES = frozenset(
    {
        "fcc_location_fabric",
        "nibrs_incident",
        "nibrs_person",
        "cms_identifiable",
        "student_record",
        "tax_identifiable",
    }
)


def validated_https_url(
    value: object,
    allowed_hosts: set[str],
    *,
    label: str = "Source",
) -> str:
    url = str(value)
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise HouseHunterError(f"{label} URL has an invalid port") from exc
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise HouseHunterError(f"{label} URL cannot use an IP address")
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in allowed_hosts
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise HouseHunterError(f"{label} URL violates the reviewed HTTPS host policy")
    return url


def validate_public_dns(
    url: str,
    *,
    label: str = "Source",
    expected_addresses: frozenset[str] | None = None,
) -> frozenset[str]:
    hostname = urlsplit(url).hostname
    if hostname is None:
        raise HouseHunterError(f"{label} hostname cannot be resolved")
    try:
        addresses = frozenset(
            item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        )
    except OSError as exc:
        raise HouseHunterError(f"{label} hostname cannot be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise HouseHunterError(f"{label} hostname resolves to a non-public address")
    if expected_addresses is not None and addresses != expected_addresses:
        raise HouseHunterError(f"{label} hostname addresses changed during acquisition")
    return addresses


def request_bounded_bytes(
    client: httpx.Client,
    url: str,
    *,
    allowed_hosts: set[str],
    max_bytes: int,
    label: str = "Source",
    params: dict[str, str] | None = None,
    expected_dns_addresses: frozenset[str] | None = None,
    validate_dns: bool = True,
) -> bytes:
    """Fetch one bounded identity response with fail-closed host and DNS policy."""
    if max_bytes <= 0:
        raise HouseHunterError(f"{label} byte limit is invalid")
    validated = validated_https_url(url, allowed_hosts, label=label)
    if validate_dns:
        validate_public_dns(
            validated,
            label=label,
            expected_addresses=expected_dns_addresses,
        )
    with client.stream(
        "GET",
        validated,
        params=params,
        follow_redirects=False,
        headers={"Accept-Encoding": "identity"},
    ) as response:
        if response.is_redirect:
            raise HouseHunterError(f"{label} returned an unexpected redirect")
        response.raise_for_status()
        content_encoding = response.headers.get("content-encoding", "identity").lower()
        if content_encoding not in {"", "identity"}:
            raise HouseHunterError(f"{label} returned an encoded response")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError as exc:
                raise HouseHunterError(f"{label} Content-Length is invalid") from exc
            if declared_bytes <= 0 or declared_bytes > max_bytes:
                raise HouseHunterError(f"{label} response size is outside its bound")
        chunks = bytearray()
        for chunk in response.iter_raw():
            chunks.extend(chunk)
            if len(chunks) > max_bytes:
                raise HouseHunterError(f"{label} response size is outside its bound")
    if not chunks:
        raise HouseHunterError(f"{label} response size is outside its bound")
    return bytes(chunks)


def deny_restricted_data_class(value: object, *, label: str = "Source") -> None:
    if value in DENIED_DATA_CLASSES:
        raise HouseHunterError(f"{label} data class is denied: {value}")


def _safe_archive_relative(value: str) -> Path:
    text = str(value)
    pure = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or "\x00" in text
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise HouseHunterError("Archive member path is unsafe")
    return Path(*pure.parts)


def _locked_archive_root(source: dict[str, Any], destination: Path) -> Path:
    archive = source.get("archive")
    if not isinstance(archive, dict):
        raise HouseHunterError("Archive metadata is invalid")
    root_name = str(archive.get("root", ""))
    valid_root = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", root_name)
    if Path(root_name).name != root_name or valid_root is None:
        raise HouseHunterError("Archive root is invalid")
    root = (destination / root_name).resolve()
    if not root.is_relative_to(destination.resolve()):
        raise HouseHunterError("Archive root escapes the extraction directory")
    return root


def _verify_extracted_archive(source: dict[str, Any], destination: Path) -> Path:
    root = _locked_archive_root(source, destination)
    if not root.is_dir() or root.is_symlink():
        raise HouseHunterError("Archive extraction is missing or unsafe")
    members = source["archive"]["members"]
    for item in members:
        relative = _safe_archive_relative(str(item["path"]))
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise HouseHunterError("Archive extraction is missing a locked member")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stat().st_size != int(item["size"]) or digest != item["sha256"]:
            raise HouseHunterError("Archive extraction checksum mismatch")
    return root


def extract_locked_archive(
    source: dict[str, Any],
    archive_path: Path,
    destination: Path,
    *,
    label: str = "Source",
) -> Path:
    """Extract exactly reviewed ZIP members with bounded expansion and no link handling."""
    archive = source["archive"]
    final = _locked_archive_root(source, destination)
    members = {
        str(item["path"]).rstrip("/"): item for item in archive["members"] if isinstance(item, dict)
    }
    expected_total = int(archive["total_uncompressed_size"])
    if expected_total != sum(int(item["size"]) for item in members.values()):
        raise HouseHunterError(f"{label} archive expansion total differs from its lock")
    if expected_total > MAX_ARCHIVE_UNCOMPRESSED or len(members) > MAX_ARCHIVE_ENTRIES:
        raise HouseHunterError(f"{label} archive exceeds extraction limits")
    if final.exists():
        return _verify_extracted_archive(source, destination)
    temporary = destination / f".{final.name}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        with zipfile.ZipFile(archive_path) as handle:
            infos = handle.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise HouseHunterError(f"{label} archive entry count exceeds its limit")
            files: dict[str, zipfile.ZipInfo] = {}
            folded: set[str] = set()
            for info in infos:
                name = info.filename.rstrip("/")
                if name.lower().endswith((".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z")):
                    raise HouseHunterError(f"{label} archive contains a nested archive")
                relative = _safe_archive_relative(name)
                normalized = relative.as_posix()
                if normalized.casefold() in folded:
                    raise HouseHunterError(
                        f"{label} archive has duplicate or case-colliding entries"
                    )
                folded.add(normalized.casefold())
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 0x1:
                    raise HouseHunterError(
                        f"{label} archive contains a link, device, or encryption"
                    )
                if not info.is_dir():
                    files[normalized] = info
            if set(files) != set(members):
                raise HouseHunterError(f"{label} archive entries differ from its reviewed lock")
            for relative, metadata in members.items():
                info = files[relative]
                if (
                    info.file_size != metadata["size"]
                    or info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO
                ):
                    raise HouseHunterError(f"{label} archive member exceeds expansion limits")
                output = temporary / _safe_archive_relative(relative)
                output.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                received = 0
                with handle.open(info) as source_handle, output.open("wb") as output_handle:
                    for chunk in iter(lambda: source_handle.read(CHUNK_SIZE), b""):
                        received += len(chunk)
                        if received > metadata["size"]:
                            raise HouseHunterError(f"{label} archive member is oversized")
                        digest.update(chunk)
                        output_handle.write(chunk)
                if received != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
                    raise HouseHunterError(f"{label} archive member checksum mismatch")
        os.replace(temporary, final)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HouseHunterError(f"Cannot extract {label} archive: {exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return final


def download_locked_file(
    url: str,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    allowed_hosts: set[str],
    max_bytes: int = DEFAULT_MAX_BYTES,
    label: str = "Source",
    client: httpx.Client | None = None,
    cancelled: Cancelled | None = None,
    validate_dns: bool = True,
) -> Path:
    if expected_size <= 0 or expected_size > max_bytes:
        raise HouseHunterError(f"{label} locked size exceeds its byte limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(120, connect=30), follow_redirects=False, trust_env=False
    )
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        current = url
        redirects = 0
        while True:
            if cancelled and cancelled():
                raise InterruptedError(f"{label} download cancelled")
            validated = validated_https_url(current, allowed_hosts, label=label)
            if validate_dns:
                validate_public_dns(validated, label=label)
            with http.stream(
                "GET",
                validated,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or redirects >= MAX_REDIRECTS:
                        raise HouseHunterError(f"{label} redirect policy failed")
                    current = urljoin(validated, location)
                    redirects += 1
                    continue
                response.raise_for_status()
                content_encoding = response.headers.get("content-encoding", "identity").lower()
                if content_encoding not in {"", "identity"}:
                    raise HouseHunterError(f"{label} uses unsupported content encoding")
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) != expected_size:
                    raise HouseHunterError(f"{label} length differs from its lock")
                received = 0
                digest = hashlib.sha256()
                with temporary.open("wb") as output:
                    for chunk in response.iter_raw(CHUNK_SIZE):
                        received += len(chunk)
                        if received > expected_size:
                            raise HouseHunterError(f"{label} stream is oversized")
                        digest.update(chunk)
                        output.write(chunk)
                break
        if temporary.stat().st_size != expected_size or digest.hexdigest() != expected_sha256:
            raise HouseHunterError(f"{label} checksum mismatch")
        os.replace(temporary, destination)
        return destination
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise HouseHunterError(f"{label} download failed: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
        if owns_client:
            http.close()
