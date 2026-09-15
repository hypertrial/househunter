from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

from househunter.errors import HouseHunterError

spec = importlib.util.spec_from_file_location(
    "househunter_generate_housing_stock_assets",
    Path(__file__).parents[1] / "scripts" / "generate_housing_stock_assets.py",
)
assert spec and spec.loader
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _contract(payload: bytes) -> dict[str, object]:
    return {
        "expected_size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "header_sha256": hashlib.sha256(payload.splitlines(keepends=True)[0]).hexdigest(),
        "required_columns": ["GEO_ID"],
    }


def test_invalid_download_never_publishes_or_leaves_a_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"GEO_ID\n1400000US01001000100\n"
    invalid = b"GEO_ID\n1400000US01001000101\n"
    destination = tmp_path / "cache" / "b25034.dat"
    monkeypatch.setattr(
        generator.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(invalid)
    )

    with pytest.raises(HouseHunterError, match="checksum differs"):
        generator.download_pinned(
            "https://example.test/b25034.dat", destination, _contract(expected)
        )

    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_valid_cached_source_never_opens_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"GEO_ID\n1400000US01001000100\n"
    destination = tmp_path / "b25034.dat"
    destination.write_bytes(payload)

    def unexpected_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access was not expected")

    monkeypatch.setattr(generator.urllib.request, "urlopen", unexpected_network)
    assert (
        generator.download_pinned(
            "https://example.test/b25034.dat", destination, _contract(payload)
        )
        == destination
    )
