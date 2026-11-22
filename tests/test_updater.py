"""Tests for drcom.updater: semver, HTTPS/mirror, SHA-256 verification, Zip Slip, apply."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from drcom import updater


class _FakeResponse:
    def __init__(self, data: bytes, headers: dict | None = None) -> None:
        self._buf = io.BytesIO(data)
        self.headers = headers or {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_version_comparison_and_prerelease() -> None:
    assert updater.is_newer("2.2.0", "2.1.1")
    assert updater.is_newer("v2.10.0", "2.2.0")
    assert updater.is_newer("2.2.0", "2.2.0-rc1")
    assert updater.is_newer("2.2.0-rc2", "2.2.0-rc1")
    assert not updater.is_newer("2.2.0", "v2.2.0")
    assert not updater.is_newer("2.1.1", "2.2.0")


def test_mirror_requires_https() -> None:
    url = "https://github.com/hZsFN/jlu-drcom-ng/releases/download/v2.2.0/app.zip"
    assert updater.apply_mirror(url, "") == url
    assert updater.apply_mirror(url, "https://mirror.example.com/") == (
        "https://mirror.example.com/" + url
    )
    with pytest.raises(ValueError):
        updater.apply_mirror(url, "http://insecure.example.com")
    with pytest.raises(ValueError):
        updater.apply_mirror("http://github.com/evil", "")


def test_check_latest_release_merges_sha256sums_and_digest(monkeypatch) -> None:
    zip_hash = "a" * 64
    exe_hash = "b" * 64
    release_json = json.dumps(
        {
            "tag_name": "v2.3.0",
            "name": "v2.3.0",
            "body": "Release notes",
            "published_at": "2026-11-22T00:00:00Z",
            "html_url": "https://github.com/hZsFN/jlu-drcom-ng/releases/tag/v2.3.0",
            "assets": [
                {
                    "name": "JLU-DrCOM-NG-2.3.0-setup.exe",
                    "browser_download_url": "https://github.com/example/setup.exe",
                    "size": 1000,
                    "digest": f"sha256:{exe_hash}",
                },
                {
                    "name": "JLU-DrCOM-NG-2.3.0-windows-x64.zip",
                    "browser_download_url": "https://github.com/example/app.zip",
                    "size": 2000,
                },
                {
                    "name": "SHA256SUMS.txt",
                    "browser_download_url": "https://github.com/example/SHA256SUMS.txt",
                    "size": 140,
                },
            ],
        }
    ).encode("utf-8")
    sums = f"{zip_hash}  JLU-DrCOM-NG-2.3.0-windows-x64.zip\n".encode("utf-8")

    def fake_urlopen(req, timeout=12.0):
        u = req.full_url
        if u.endswith("SHA256SUMS.txt"):
            return _FakeResponse(sums)
        return _FakeResponse(release_json)

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
    has_upd, info = updater.check_latest_release(current_version="2.2.0")
    assert has_upd
    assert info.version == "2.3.0"

    inst = updater.select_asset(info, install_kind="windows_installer")
    port = updater.select_asset(info, install_kind="windows_portable")
    assert inst is not None and inst.sha256 == exe_hash
    assert port is not None and port.sha256 == zip_hash


def test_download_and_verify_enforces_sha256(tmp_path, monkeypatch) -> None:
    payload = b"hello-drcom-release-bytes"
    good_hash = hashlib.sha256(payload).hexdigest()
    bad_hash = "0" * 64

    asset_no_hash = updater.ReleaseAsset(
        name="app.zip", url="https://example.com/app.zip", size=len(payload), sha256=""
    )
    with pytest.raises(ValueError):
        updater.download_and_verify(asset_no_hash, tmp_path)

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda req, timeout=120: _FakeResponse(payload)
    )
    asset_bad = updater.ReleaseAsset(
        name="app.zip", url="https://example.com/app.zip", size=len(payload), sha256=bad_hash
    )
    with pytest.raises(ValueError):
        updater.download_and_verify(asset_bad, tmp_path)
    assert not (tmp_path / "app.zip").exists()
    assert not (tmp_path / "app.zip.part").exists()

    asset_good = updater.ReleaseAsset(
        name="app.zip", url="https://example.com/app.zip", size=len(payload), sha256=good_hash
    )
    out = updater.download_and_verify(asset_good, tmp_path)
    assert out.exists() and out.read_bytes() == payload


def test_safe_extract_blocks_path_traversal_and_symlinks(tmp_path) -> None:
    bad_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.txt", "pwn")
    with pytest.raises(ValueError):
        updater._safe_extract_zip(bad_zip, tmp_path / "out_zip")

    bad_tar = tmp_path / "evil.tar.gz"
    with tarfile.open(bad_tar, "w:gz") as tf:
        ti = tarfile.TarInfo("../escape.sh")
        data = b"#!/bin/sh\n"
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    with pytest.raises(ValueError):
        updater._safe_extract_tar_gz(bad_tar, tmp_path / "out_tar")

    symlink_tar = tmp_path / "symlink.tar.gz"
    with tarfile.open(symlink_tar, "w:gz") as tf:
        ti = tarfile.TarInfo("link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)
    with pytest.raises(ValueError):
        updater._safe_extract_tar_gz(symlink_tar, tmp_path / "out_sym")


def test_apply_update_handlers_mocked(tmp_path, monkeypatch) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    pkg_exe = tmp_path / "setup.exe"
    pkg_exe.write_bytes(b"MZ")

    # source mode refuses auto overwrite
    res_src = updater.apply_update(pkg_exe, install_kind="source", app_dir=app_dir)
    assert res_src["started"] is False
    assert res_src["kind"] == "source"

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        updater.subprocess, "Popen", lambda args, **_kw: spawned.append(list(args)) or object()
    )

    # windows_installer spawns /SP- /SILENT /CLOSEAPPLICATIONS /relaunch=1
    res_inst = updater.apply_update(
        pkg_exe, install_kind="windows_installer", app_dir=app_dir, restart=True
    )
    assert res_inst["started"] is True
    assert res_inst["should_exit"] is True
    assert "/SILENT" in spawned[-1] and "/relaunch=1" in spawned[-1]

    # linux_bundle extracts tarball cleanly and spawns sh helper
    tar_path = tmp_path / "JLU-DrCOM-NG-2.3.0-linux-x86_64.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        data = b"ELF"
        ti = tarfile.TarInfo("JLU-DrCOM-NG-2.3.0/drcomd")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    res_linux = updater.apply_update(tar_path, install_kind="linux_bundle", app_dir=app_dir)
    assert res_linux["started"] is True
    assert res_linux["should_exit"] is True
    assert spawned[-1][0] == "/bin/sh"
