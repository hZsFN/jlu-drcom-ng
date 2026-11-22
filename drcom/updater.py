"""GitHub Releases self-update check, verified download, and platform apply.

Pure standard library (no third-party HTTP dependencies) so updates keep
working even when optional UI or crypto packages are missing or degraded.

Security invariants:
- Both API lookups and asset downloads require ``https://`` URLs (any
  configured ``mirror_prefix`` must also use ``https://``).
- Every downloaded archive or installer MUST match a 64-hex SHA-256 digest
  obtained from ``SHA256SUMS.txt`` or the GitHub Release asset ``digest``
  metadata before it is moved into place or executed.
- Archive extractors (ZIP / tar.gz) validate member paths against path
  traversal (Zip Slip) and reject absolute paths or out-of-tree links.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__

GITHUB_OWNER = "hZsFN"
GITHUB_REPO = "jlu-drcom-ng"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"

_VERSION_RE = re.compile(
    r"^\s*v?(?P<core>\d+(?:\.\d+)*)(?:[-+.]?(?P<pre_tag>alpha|beta|rc|a|b)[.-]?(?P<pre_num>\d*))?",
    re.IGNORECASE,
)
_PRE_RANK = {"a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3}
_SHA256_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$")
_INNO_APP_ID = "{8F3A2B14-6C5D-4E7A-9B21-3D4F5E6A7B8C}_is1"


@dataclass
class ReleaseAsset:
    name: str
    url: str
    size: int = 0
    sha256: str = ""


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    name: str
    body: str
    published_at: str
    html_url: str
    assets: list[ReleaseAsset] = field(default_factory=list)
    checksums: dict[str, str] = field(default_factory=dict)


@dataclass
class UpdateProgress:
    stage: str
    downloaded_bytes: int = 0
    total_bytes: int = 0
    message: str = ""


# --------------------------------------------------------------------------
# Version helpers
# --------------------------------------------------------------------------
def parse_version(raw: str) -> tuple[tuple[int, ...], int, int, int]:
    """Parse ``raw`` into a comparable tuple ``(core_nums, is_stable, pre_rank, pre_num)``."""
    match = _VERSION_RE.match(raw or "")
    if not match:
        return ((0,), 0, 0, 0)
    core = tuple(int(part) for part in match.group("core").split("."))
    # Pad/trim trailing zeros? Keep at least 3 components for comparison consistency.
    if len(core) < 3:
        core = core + (0,) * (3 - len(core))
    pre_tag = (match.group("pre_tag") or "").lower()
    if not pre_tag:
        return (core, 1, 99, 0)
    pre_num = int(match.group("pre_num") or "0")
    return (core, 0, _PRE_RANK.get(pre_tag, 0), pre_num)


def is_newer(latest: str, current: str) -> bool:
    """Return True iff *latest* is strictly newer than *current*."""
    return parse_version(latest) > parse_version(current)


# --------------------------------------------------------------------------
# Install kind & URL helpers
# --------------------------------------------------------------------------
def detect_install_kind(app_dir: Path | None = None) -> str:
    """Classify the running deployment:
    ``windows_installer`` / ``windows_portable`` / ``linux_bundle`` / ``source``.
    """
    if app_dir is None:
        if not getattr(sys, "frozen", False):
            return "source"
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(app_dir).resolve()

    if sys.platform == "win32":
        if (base_dir / "unins000.exe").exists() or any(base_dir.glob("unins*.exe")):
            return "windows_installer"
        if _inno_registry_matches(base_dir):
            return "windows_installer"
        return "windows_portable"

    if sys.platform.startswith("linux"):
        return "linux_bundle"

    return "source"


def _inno_registry_matches(base_dir: Path) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    subkey = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{_INNO_APP_ID}"
    resolved = str(base_dir).rstrip("\\/").lower()
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, subkey) as key:
                val, _ = winreg.QueryValueEx(key, "InstallLocation")
                if str(val).rstrip("\\/").lower() == resolved:
                    return True
        except OSError:
            continue
    return False


def _ensure_https_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"出于安全考虑仅允许 HTTPS 更新地址：{url}")
    return url.strip()


def apply_mirror(url: str, mirror_prefix: str = "") -> str:
    """Optionally prepend a user-configured HTTPS mirror prefix to *url*."""
    clean_url = _ensure_https_url(url)
    prefix = (mirror_prefix or "").strip()
    if not prefix:
        return clean_url
    parsed_prefix = urllib.parse.urlsplit(prefix)
    if parsed_prefix.scheme.lower() != "https" or not parsed_prefix.netloc:
        raise ValueError(f"镜像前缀必须使用 HTTPS 协议：{prefix}")
    if clean_url.startswith(prefix):
        return clean_url
    return prefix.rstrip("/") + "/" + clean_url


def _request(url: str, *, accept: str = "*/*", timeout: float = 12.0) -> urllib.request.Request:
    _ensure_https_url(url)
    return urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": f"JLU-DrCOM-NG/{__version__}",
        },
        method="GET",
    )


def _fetch_bytes(url: str, *, accept: str = "*/*", timeout: float = 12.0,
                 max_bytes: int = 4 * 1024 * 1024) -> bytes:
    req = _request(url, accept=accept, timeout=timeout)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"响应体过大，超过 {max_bytes} 字节限制")
    return data


def _parse_sha256sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _SHA256_LINE_RE.match(line)
        if not m:
            continue
        digest = m.group(1).lower()
        filename = Path(m.group(2).strip()).name
        if filename:
            out[filename] = digest
    return out


# --------------------------------------------------------------------------
# Release lookup & asset selection
# --------------------------------------------------------------------------
def check_latest_release(
    *,
    current_version: str = __version__,
    mirror_prefix: str = "",
    timeout: float = 12.0,
    api_url: str = LATEST_RELEASE_API,
) -> tuple[bool, ReleaseInfo]:
    """Query GitHub Releases and return ``(has_update, ReleaseInfo)``."""
    target_api = apply_mirror(api_url, mirror_prefix)
    raw = _fetch_bytes(target_api, accept="application/vnd.github+json", timeout=timeout)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub Release API 返回格式无效")

    tag = str(payload.get("tag_name") or "").strip()
    version = tag.lstrip("vV") or str(payload.get("name") or "").strip().lstrip("vV")
    assets: list[ReleaseAsset] = []
    sha_asset_url = ""

    for item in payload.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = Path(str(item.get("name") or "")).name
        dl_url = str(item.get("browser_download_url") or "").strip()
        if not name or not dl_url:
            continue
        raw_digest = str(item.get("digest") or "").strip().lower()
        if raw_digest.startswith("sha256:"):
            raw_digest = raw_digest[len("sha256:"):]
        if len(raw_digest) != 64 or not re.fullmatch(r"[0-9a-f]{64}", raw_digest):
            raw_digest = ""
        assets.append(
            ReleaseAsset(
                name=name,
                url=dl_url,
                size=int(item.get("size") or 0),
                sha256=raw_digest,
            )
        )
        if name in ("SHA256SUMS.txt", "SHA256SUMS"):
            sha_asset_url = dl_url

    checksums: dict[str, str] = {}
    if sha_asset_url:
        try:
            sums_url = apply_mirror(sha_asset_url, mirror_prefix)
            sums_bytes = _fetch_bytes(sums_url, timeout=timeout, max_bytes=256 * 1024)
            checksums = _parse_sha256sums(sums_bytes.decode("utf-8", errors="replace"))
        except Exception:
            checksums = {}

    for asset in assets:
        if not asset.sha256 and asset.name in checksums:
            asset.sha256 = checksums[asset.name]

    info = ReleaseInfo(
        version=version,
        tag=tag,
        name=str(payload.get("name") or tag),
        body=str(payload.get("body") or ""),
        published_at=str(payload.get("published_at") or ""),
        html_url=str(payload.get("html_url") or RELEASES_PAGE_URL),
        assets=assets,
        checksums=checksums,
    )
    return (is_newer(info.version, current_version), info)


def _norm_arch(machine: str | None = None) -> str:
    m = (machine or platform.machine() or "").lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m or "x86_64"


def select_asset(
    info: ReleaseInfo,
    *,
    install_kind: str | None = None,
    machine: str | None = None,
) -> ReleaseAsset | None:
    """Pick the most suitable release asset for this install kind and arch."""
    kind = install_kind or detect_install_kind()
    arch = _norm_arch(machine)
    assets = [a for a in info.assets if a.name not in ("SHA256SUMS.txt", "SHA256SUMS")]
    if not assets:
        return None

    def _find(predicate: Callable[[ReleaseAsset], bool]) -> ReleaseAsset | None:
        for asset in assets:
            if predicate(asset):
                return asset
        return None

    if kind == "windows_installer":
        return (
            _find(lambda a: a.name.lower().endswith("-setup.exe") or (
                a.name.lower().endswith(".exe") and "setup" in a.name.lower()
            ))
            or _find(lambda a: "windows" in a.name.lower() and a.name.lower().endswith(".zip"))
        )

    if kind == "windows_portable":
        return (
            _find(lambda a: "windows" in a.name.lower() and a.name.lower().endswith(".zip"))
            or _find(lambda a: a.name.lower().endswith(".exe") and "setup" in a.name.lower())
        )

    if kind == "linux_bundle":
        arch_tokens = ("x86_64", "amd64", "x64") if arch == "x86_64" else ("arm64", "aarch64")
        return (
            _find(
                lambda a: a.name.startswith("JLU-DrCOM-NG")
                and "linux" in a.name.lower()
                and a.name.lower().endswith(".tar.gz")
                and any(t in a.name.lower() for t in arch_tokens)
            )
            or _find(
                lambda a: a.name.startswith("JLU-DrCOM-NG")
                and "linux" in a.name.lower()
                and a.name.lower().endswith(".tar.gz")
            )
            or _find(lambda a: "linux" in a.name.lower() and a.name.lower().endswith(".tar.gz"))
        )

    # source: suggest platform archive for inspection, apply_update still keeps source untouched
    if sys.platform == "win32":
        return (
            _find(lambda a: "windows" in a.name.lower() and a.name.lower().endswith(".zip"))
            or _find(lambda a: "setup" in a.name.lower() and a.name.lower().endswith(".exe"))
        )
    if sys.platform.startswith("linux"):
        return _find(lambda a: a.name.startswith("JLU-DrCOM-NG") and "linux" in a.name.lower())
    return assets[0]


# --------------------------------------------------------------------------
# Download & SHA-256 verification
# --------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def download_and_verify(
    asset: ReleaseAsset,
    dest_dir: Path,
    *,
    expected_sha256: str = "",
    mirror_prefix: str = "",
    timeout: float = 120.0,
    progress_cb: Callable[[UpdateProgress], None] | None = None,
) -> Path:
    """Download *asset* to *dest_dir* and enforce SHA-256 verification."""
    digest = (expected_sha256 or asset.sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(
            f"资产 {asset.name} 缺少有效的 SHA-256 校验值，已拒绝下载。"
        )

    safe_name = Path(asset.name).name
    if not safe_name or safe_name in (".", ".."):
        raise ValueError(f"非法文件名：{asset.name!r}")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = dest_dir / safe_name

    # Reuse already-verified download if present on disk
    if final_path.exists():
        try:
            if sha256_file(final_path) == digest:
                if progress_cb:
                    size = final_path.stat().st_size
                    progress_cb(UpdateProgress("verified", size, size, "已复用本地校验通过的安装包"))
                return final_path
        except OSError:
            pass

    part_path = dest_dir / f"{safe_name}.part"
    url = apply_mirror(asset.url, mirror_prefix)
    req = _request(url, timeout=timeout)

    hasher = hashlib.sha256()
    downloaded = 0
    total = asset.size
    if progress_cb:
        progress_cb(UpdateProgress("downloading", 0, total, f"正在下载 {safe_name}…"))

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if total <= 0:
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit():
                    total = int(cl)
            with open(part_path, "wb") as out:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(UpdateProgress("downloading", downloaded, total, f"已下载 {safe_name}"))
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    actual = hasher.hexdigest().lower()
    if actual != digest:
        part_path.unlink(missing_ok=True)
        raise ValueError(
            f"SHA-256 校验失败（预期 {digest[:16]}…，实际 {actual[:16]}…），已删除损坏文件"
        )

    os.replace(part_path, final_path)
    if progress_cb:
        progress_cb(UpdateProgress("verified", downloaded, total or downloaded, "SHA-256 校验通过"))
    return final_path


# --------------------------------------------------------------------------
# Safe archive extraction & platform install
# --------------------------------------------------------------------------
def _safe_extract_zip(archive: Path, dest_dir: Path) -> Path:
    dest_root = dest_dir.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"压缩包包含危险路径：{member.filename}")
            target = (dest_root / member_path).resolve()
            if not target.is_relative_to(dest_root):
                raise ValueError(f"压缩包包含越界路径：{member.filename}")
            # Reject symlinks encoded in unix external_attr
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"压缩包包含符号链接，已拒绝解压：{member.filename}")
        zf.extractall(dest_root)
    return _unwrap_single_root(dest_root)


def _safe_extract_tar_gz(archive: Path, dest_dir: Path) -> Path:
    dest_root = dest_dir.resolve()
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"压缩包包含危险路径：{member.name}")
            if member.issym() or member.islnk() or member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                raise ValueError(f"压缩包包含不支持的特殊文件：{member.name}")
            target = (dest_root / member_path).resolve()
            if not target.is_relative_to(dest_root):
                raise ValueError(f"压缩包包含越界路径：{member.name}")
        extract_kwargs: dict[str, Any] = {}
        if sys.version_info >= (3, 12):
            extract_kwargs["filter"] = "data"
        tf.extractall(dest_root, **extract_kwargs)
    return _unwrap_single_root(dest_root)


def _unwrap_single_root(dest_root: Path) -> Path:
    entries = [p for p in dest_root.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest_root


def _spawn_windows_portable_swap(
    source_dir: Path,
    app_dir: Path,
    *,
    exe_path: Path | None,
    restart: bool,
    wait_pid: int,
) -> Path:
    script_fd, script_raw = tempfile.mkstemp(prefix="drcom-update-", suffix=".cmd")
    os.close(script_fd)
    script_path = Path(script_raw)
    relaunch_line = (
        f'if exist "{exe_path}" start "" "{exe_path}"\r\n'
        if (restart and exe_path is not None)
        else ""
    )
    script = (
        "@echo off\r\n"
        "setlocal\r\n"
        f":waitloop\r\n"
        f'tasklist /FI "PID eq {wait_pid}" 2>nul | find "{wait_pid}" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "    timeout /t 1 /nobreak >nul\r\n"
        "    goto waitloop\r\n"
        ")\r\n"
        "taskkill /F /IM drcomd.exe >nul 2>&1\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        f'xcopy /E /Y /I /Q "{source_dir}\\*" "{app_dir}\\" >nul\r\n'
        f'rmdir /S /Q "{source_dir.parent}" >nul 2>&1\r\n'
        f"{relaunch_line}"
        'del "%~f0" >nul 2>&1\r\n'
    )
    script_path.write_text(script, encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        ["cmd.exe", "/c", str(script_path)],
        creationflags=flags,
        close_fds=True,
    )
    return script_path


def _spawn_linux_bundle_swap(
    source_dir: Path,
    app_dir: Path,
    *,
    exe_path: Path | None,
    restart: bool,
    wait_pid: int,
) -> Path:
    script_fd, script_raw = tempfile.mkstemp(prefix="drcom-update-", suffix=".sh")
    os.close(script_fd)
    script_path = Path(script_raw)
    import shlex

    src_q = shlex.quote(str(source_dir))
    stage_root_q = shlex.quote(str(source_dir.parent))
    app_q = shlex.quote(str(app_dir))
    self_q = shlex.quote(str(script_path))
    relaunch = ""
    if restart and exe_path is not None:
        exe_q = shlex.quote(str(exe_path))
        relaunch = f'if [ -x {exe_q} ]; then nohup {exe_q} >/dev/null 2>&1 & fi\n'

    script = (
        "#!/bin/sh\n"
        f"while kill -0 {wait_pid} 2>/dev/null; do sleep 0.3; done\n"
        "pkill -x drcomd >/dev/null 2>&1 || true\n"
        "sleep 0.3\n"
        f"mkdir -p {app_q}\n"
        f"cp -af {src_q}/. {app_q}/\n"
        f"rm -rf {stage_root_q}\n"
        f"{relaunch}"
        f"rm -f {self_q}\n"
    )
    script_path.write_text(script, encoding="utf-8")
    os.chmod(script_path, 0o700)
    subprocess.Popen(
        ["/bin/sh", str(script_path)],
        start_new_session=True,
        close_fds=True,
    )
    return script_path


def apply_update(
    package_path: Path,
    *,
    install_kind: str | None = None,
    app_dir: Path | None = None,
    restart: bool = True,
) -> dict[str, Any]:
    """Launch installer or schedule safe file replacement for *package_path*.

    The caller should shut down local daemons/engines and exit promptly once
    ``should_exit`` is True in the returned status dict.
    """
    pkg = Path(package_path).resolve()
    if not pkg.exists():
        raise FileNotFoundError(f"更新包不存在：{pkg}")

    kind = install_kind or detect_install_kind(app_dir)
    if kind == "source":
        return {
            "started": False,
            "should_exit": False,
            "kind": "source",
            "message": f"当前以源码方式运行，请通过 git pull 更新，或手动打开 {pkg}",
        }

    resolved_app_dir = (
        Path(app_dir).resolve()
        if app_dir is not None
        else Path(sys.executable).resolve().parent
    )
    exe_path = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None

    if kind == "windows_installer" and pkg.suffix.lower() == ".exe":
        args = [str(pkg), "/SP-", "/SILENT", "/CLOSEAPPLICATIONS"]
        if restart:
            args.append("/relaunch=1")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(args, creationflags=flags, close_fds=True)
        return {
            "started": True,
            "should_exit": True,
            "kind": kind,
            "message": "已启动安装程序完成自动更新，程序即将退出并重启…",
        }

    if pkg.suffix.lower() == ".zip":
        stage_dir = Path(tempfile.mkdtemp(prefix="drcom-stage-", dir=str(pkg.parent)))
        try:
            content_root = _safe_extract_zip(pkg, stage_dir)
        except Exception:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise
        if sys.platform == "win32":
            _spawn_windows_portable_swap(
                content_root,
                resolved_app_dir,
                exe_path=exe_path,
                restart=restart,
                wait_pid=os.getpid(),
            )
            return {
                "started": True,
                "should_exit": True,
                "kind": kind,
                "message": "更新包已校验并解压，正在后台替换文件…",
            }
        shutil.rmtree(stage_dir, ignore_errors=True)
        return {
            "started": False,
            "should_exit": False,
            "kind": kind,
            "message": f"无法在当前系统自动安装 ZIP 包：{pkg}",
        }

    if pkg.name.lower().endswith(".tar.gz"):
        stage_dir = Path(tempfile.mkdtemp(prefix="drcom-stage-", dir=str(pkg.parent)))
        try:
            content_root = _safe_extract_tar_gz(pkg, stage_dir)
        except Exception:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise
        _spawn_linux_bundle_swap(
            content_root,
            resolved_app_dir,
            exe_path=exe_path,
            restart=restart,
            wait_pid=os.getpid(),
        )
        return {
            "started": True,
            "should_exit": True,
            "kind": kind,
            "message": "Linux 更新包已校验并解压，正在后台替换并重启…",
        }

    return {
        "started": False,
        "should_exit": False,
        "kind": kind,
        "message": f"未识别的更新包格式：{pkg.name}",
    }


__all__ = [
    "GITHUB_OWNER",
    "GITHUB_REPO",
    "LATEST_RELEASE_API",
    "RELEASES_PAGE_URL",
    "ReleaseAsset",
    "ReleaseInfo",
    "UpdateProgress",
    "apply_mirror",
    "apply_update",
    "check_latest_release",
    "detect_install_kind",
    "download_and_verify",
    "is_newer",
    "parse_version",
    "select_asset",
    "sha256_file",
]
