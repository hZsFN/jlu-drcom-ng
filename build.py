# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller packaging.

Two goals, in order:

1. **Everything self-contained.**  A ``--onedir`` build that ships every Python
   dependency *and* the Flet desktop runtime, so the result runs on a machine
   with no network and no Python.  Flet normally downloads a ~40 MB Flutter
   client on first launch; we vendor that archive (``vendor/flet-windows.zip``)
   and drop it where ``flet_desktop`` looks for a bundled client, which turns
   that download into a local extract.
2. **A single file**, for when a folder is inconvenient.

Usage::

    python build.py                 # folder build, fully self-contained (default)
    python build.py --onefile       # single .exe instead
    python build.py --console       # keep a console window, for troubleshooting
    python build.py --fetch-runtime # download the Flet runtime into vendor/ first

Notes
-----
* ``flet`` and ``pystray`` import their backends dynamically, so PyInstaller's
  static analysis misses them — hence the explicit ``--hidden-import`` list.
* ``--exclude-module`` keeps numpy/pandas/torch and friends out of the bundle;
  without it the build balloons by hundreds of megabytes.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"

#: Where the vendored Flet client archive lives.
FLET_ARCHIVE = VENDOR / "flet-windows.zip"

# Runtime-dynamic imports that static analysis cannot see.
HIDDEN_IMPORTS = [
    "pystray",
    "pystray._win32",
    "pystray._darwin",
    "pystray._xorg",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "flet",
    "flet.canvas",
    "flet.controls.painting",
    "flet.controls.box",
    "flet_desktop",
    "drcom",
]

# Keep the bundle lean: none of these are used, and several are enormous.
EXCLUDES = [
    "tkinter.test",
    "test",
    "unittest",
    "pytest",
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "cv2",
    "torch",
    "easyocr",
]


def fetch_flet_runtime() -> Path | None:
    """Download the Flet desktop client archive into ``vendor/``.

    The URL and filename come from the installed ``flet_desktop`` package, so
    this always matches the version actually in use.
    """
    try:
        import flet_desktop
    except ImportError:
        print("flet_desktop is not installed; cannot fetch the runtime", file=sys.stderr)
        return None

    version = flet_desktop.version.version
    filename = flet_desktop.get_artifact_filename()
    url = f"https://github.com/flet-dev/flet/releases/download/v{version}/{filename}"

    VENDOR.mkdir(parents=True, exist_ok=True)
    target = VENDOR / filename
    if target.exists() and target.stat().st_size > 1_000_000:
        print(f"runtime already vendored: {target} ({target.stat().st_size / 1048576:.1f} MB)")
        return target

    print(f"downloading {url}")
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url) as response, temporary.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done / 1048576:6.1f} / {total / 1048576:.1f} MB", end="")
        print()
        temporary.replace(target)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        print(f"download failed: {exc}", file=sys.stderr)
        return None

    write_fingerprint_sidecar(target)
    return target


def write_fingerprint_sidecar(archive: Path) -> None:
    """Write the ``<archive>.sha256`` sidecar ``flet_desktop`` looks for.

    With it, the runtime skips re-hashing 40 MB on every launch.
    """
    sidecar = archive.with_name(archive.name + ".sha256")
    size = archive.stat().st_size
    if sidecar.exists():
        recorded = sidecar.read_text(encoding="ascii").split()
        if len(recorded) == 2 and recorded[1] == str(size):
            return  # still valid, do not re-hash
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar.write_text(f"{digest} {size}\n", encoding="ascii")
    print(f"wrote fingerprint {sidecar.name}")


def build(*, onefile: bool = False, name: str = "DrCOM-JLU", console: bool = False) -> int:
    arguments = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if onefile else "--onedir",
        "--name", name,
        "--console" if console else "--windowed",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]

    for module in HIDDEN_IMPORTS:
        arguments += ["--hidden-import", module]
    for module in EXCLUDES:
        arguments += ["--exclude-module", module]

    # Package *data* files.  ``flet`` reads its icon tables from JSON at
    # runtime rather than importing them, so a plain build dies with
    # "FileNotFoundError: .../flet/controls/material/icons.json" the moment any
    # control with an icon is constructed.  Static analysis cannot see those.
    for package in ("flet", "flet_desktop", "PIL"):
        arguments += ["--collect-data", package]

    # Ship the Flet runtime inside the package so no download is needed.
    runtime = FLET_ARCHIVE if FLET_ARCHIVE.exists() else None
    if runtime is None:
        for candidate in VENDOR.glob("flet-*.zip"):
            runtime = candidate
            break
    if runtime is not None:
        write_fingerprint_sidecar(runtime)
        arguments += ["--add-data", f"{runtime}{';' if sys.platform == 'win32' else ':'}flet_desktop/app"]
        sidecar = runtime.with_name(runtime.name + ".sha256")
        if sidecar.exists():
            arguments += [
                "--add-data",
                f"{sidecar}{';' if sys.platform == 'win32' else ':'}flet_desktop/app",
            ]
        print(f"bundling Flet runtime: {runtime.name} ({runtime.stat().st_size / 1048576:.1f} MB)")
    else:
        print(
            "WARNING: no vendored Flet runtime found.\n"
            "         The build will still work, but the first launch on a fresh\n"
            "         machine must download ~40 MB.  Run with --fetch-runtime to\n"
            "         vendor it now.",
            file=sys.stderr,
        )

    icon = ROOT / "assets" / "app.ico"
    if icon.exists():
        arguments += ["--icon", str(icon)]

    for extra in ("README.md", "README_CN.md"):
        path = ROOT / extra
        if path.exists():
            arguments += ["--add-data", f"{path}{';' if sys.platform == 'win32' else ':'}."]

    arguments.append(str(ROOT / "main.py"))

    print("running PyInstaller ...")
    return subprocess.call(arguments)


def main() -> int:
    parser = argparse.ArgumentParser(description="Package the DrCOM JLU client")
    parser.add_argument("--onefile", action="store_true",
                        help="single .exe instead of a folder")
    parser.add_argument("--name", default="DrCOM-JLU", help="output name")
    parser.add_argument("--console", action="store_true",
                        help="keep a console window (troubleshooting)")
    parser.add_argument("--fetch-runtime", action="store_true",
                        help="download the Flet desktop runtime into vendor/ first")
    parser.add_argument("--fetch-only", action="store_true",
                        help="only fetch the runtime, do not build")
    options = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is missing. Install it with: pip install pyinstaller", file=sys.stderr)
        return 2

    if options.fetch_runtime or options.fetch_only:
        if fetch_flet_runtime() is None:
            return 1
        if options.fetch_only:
            return 0

    return build(onefile=options.onefile, name=options.name, console=options.console)


if __name__ == "__main__":
    raise SystemExit(main())
