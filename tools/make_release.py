#!/usr/bin/env python3
"""Build the release artefacts for JLU DrCOM NG.

Keeps the naming in one place.  The artefacts drifted twice during the first
release (source-only, then a missing executable, then a renamed source
archive), which is what this exists to prevent.

    python tools/make_release.py                 # checksums only
    python tools/make_release.py --with-bundle   # + the Windows bundle (needs dist/)
    python tools/make_release.py --upload        # build, then hand over to gh

Artefacts:

    JLU-DrCOM-NG-<version>-windows-x64.zip     dist/ bundle, Python + Flet inside
    SHA256SUMS.txt                             digest of it

No source archive: GitHub generates "Source code (zip)" and "(tar.gz)" for every
tag automatically, from the same ``git archive`` this used to run, so shipping
our own copy only duplicated an asset and the checksum line that goes with it.

Uploading is deliberately *not* automatic unless ``--upload`` is passed: this
writes files and prints the exact command, so a human sees what is going out.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / "release"
DIST_DIR = ROOT / "dist" / "JLU-DrCOM-NG"
PACKAGE_NAME = "JLU-DrCOM-NG"


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def tag_version() -> str:
    """The version from the newest tag, e.g. ``v1.0.0`` -> ``1.0.0``."""
    tag = run(["git", "describe", "--tags", "--abbrev=0"]).stdout.strip()
    return tag.lstrip("v")


def source_version() -> str:
    """``__version__`` from drcom/__init__.py, the single source of truth."""
    init = ROOT / "drcom" / "__init__.py"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def current_version() -> str:
    """Prefer drcom/__init__.py over the tag.

    The tag is moved to the release commit *after* the build, so reading it first
    names the artefacts after the previous version -- exactly what happened on
    2026-10-31 (a "1.1.0" installer containing 1.2.0 code) and again on
    2026-11-02 (a "1.3.0" bundle built from 2.0.0 sources).
    """
    tv, sv = tag_version(), source_version()
    if tv and sv and tv != sv:
        print(
            f"note: git tag says {tv}, drcom/__init__.py says {sv} — using the source version",
            file=sys.stderr,
        )
    version = sv or tv
    if not version:
        raise SystemExit("cannot determine version: no git tag and no __version__ in drcom/__init__.py")
    return version


def ensure_clean() -> None:
    status = run(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        print("working tree is not clean:\n" + status, file=sys.stderr)
        print("\ncommit or stash first — the source archive is built from the tag,"
              " but a dirty tree usually means the tag is behind.", file=sys.stderr)
        raise SystemExit(1)


def _linux_arch() -> str:
    machine = (platform.machine() or "").lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return "x86_64"


def build_bundle_zip(version: str) -> pathlib.Path:
    """The Windows bundle, zipped as a single-rooted portable app folder."""
    if not DIST_DIR.exists():
        raise SystemExit(
            f"{DIST_DIR} not found -- run `python build.py` first "
            "(add --fetch-runtime if the Flet runtime is not vendored yet)"
        )

    target = RELEASE_DIR / f"{PACKAGE_NAME}-{version}-windows-x64.zip"
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    prefix = f"{PACKAGE_NAME}-{version}/"
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(DIST_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, prefix + str(path.relative_to(DIST_DIR)).replace("\\", "/"))
                count += 1
    size_mb = target.stat().st_size / 1048576
    print(f"  bundle  {target.name}  ({count} files, {size_mb:.1f} MB)")
    return target


def build_linux_bundle_tarball(version: str) -> pathlib.Path:
    """The Linux desktop bundle (``dist/JLU-DrCOM-NG``) + install helper."""
    if not DIST_DIR.exists():
        raise SystemExit(
            f"{DIST_DIR} not found -- run `python build.py` first "
            "(add --fetch-runtime if the Flet runtime is not vendored yet)"
        )
    arch = _linux_arch()
    target = RELEASE_DIR / f"{PACKAGE_NAME}-{version}-linux-{arch}.tar.gz"
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    prefix = f"{PACKAGE_NAME}-{version}"
    count = 0
    with tarfile.open(target, "w:gz") as archive:
        for path in sorted(DIST_DIR.rglob("*")):
            if path.is_file() or path.is_symlink():
                rel = str(path.relative_to(DIST_DIR)).replace("\\", "/")
                archive.add(path, arcname=f"{prefix}/{rel}", recursive=False)
                count += 1
        for helper in ("install.sh", "jlu-drcom-ng.service.in"):
            hp = ROOT / "installer" / "linux" / helper
            if hp.exists():
                archive.add(hp, arcname=f"{prefix}/{helper}")
                count += 1
    size_mb = target.stat().st_size / 1048576
    print(f"  bundle  {target.name}  ({count} files, {size_mb:.1f} MB)")
    return target


def build_linux_daemon_tarball(version: str) -> pathlib.Path | None:
    """Standalone Linux native core (``drcomd``) tarball if compiled."""
    drcomd_bin = ROOT / "native" / "drcomd"
    if not drcomd_bin.exists():
        return None
    arch = _linux_arch()
    target = RELEASE_DIR / f"drcomd-{version}-linux-{arch}.tar.gz"
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    prefix = f"drcomd-{version}"
    with tarfile.open(target, "w:gz") as archive:
        archive.add(drcomd_bin, arcname=f"{prefix}/drcomd")
        for extra_src, arc_rel in (
            (ROOT / "LICENSE", "LICENSE"),
            (ROOT / "native" / "README.md", "README.md"),
            (ROOT / "installer" / "linux" / "install.sh", "install.sh"),
            (ROOT / "installer" / "linux" / "jlu-drcom-ng.service.in", "jlu-drcom-ng.service.in"),
        ):
            if extra_src.exists():
                archive.add(extra_src, arcname=f"{prefix}/{arc_rel}")
    size_kb = target.stat().st_size / 1024
    print(f"  daemon  {target.name}  ({size_kb:.1f} KB)")
    return target


def find_installers(version: str) -> list[pathlib.Path]:
    """setup.exe files already sitting in release/ for this version.

    They are built by ``tools/make_installer.py``, not here, but they get
    uploaded to the same release — so they belong in the checksum file.
    """
    found = sorted(RELEASE_DIR.glob(f"{PACKAGE_NAME}-{version}-*setup.exe"))
    # The archived Dart build carries the same version number and a "-dart" suffix.
    # It is not shipped any more (see the dart-2.0.0-archive tag), so a leftover
    # copy in release/ must not be swept into this release.
    found = [p for p in found if "-dart" not in p.name]
    for path in found:
        print(f"  installer  {path.name}  ({path.stat().st_size / 1048576:.1f} MB)")
    return found


def write_checksums(
    paths: list[pathlib.Path], *, tag: str = "", merge_existing: bool = False
) -> pathlib.Path:
    """Digests of what we upload, plus a pointer to the source GitHub adds."""
    target = RELEASE_DIR / "SHA256SUMS.txt"
    digests: dict[str, str] = {}
    if merge_existing and target.exists():
        for raw_line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$", raw_line)
            if m:
                digests[m.group(2)] = m.group(1).lower()
    for path in paths:
        digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()

    lines = [f"{digest}  {name}" for name, digest in digests.items()]
    if tag:
        lines.append("")
        lines.append(
            "# 源码不用单独下载：release 页面 Assets 里 GitHub 自动生成的 "
            f"Source code (zip) / (tar.gz) 就是 {tag} 这个 tag 的源码。"
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  hashes  {target.name}")
    for line in lines:
        print(f"            {line}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-bundle", action="store_true",
                        help="also archive dist/ (requires a completed build)")
    parser.add_argument("--with-drcomd", action="store_true",
                        help="also archive standalone native/drcomd on Linux")
    parser.add_argument("--merge-checksums", action="store_true",
                        help="preserve existing entries in SHA256SUMS.txt when adding new assets")
    parser.add_argument("--tag-override", default="",
                        help="override release tag (e.g. v2.2.0)")
    parser.add_argument("--upload", action="store_true",
                        help="upload the artefacts to the GitHub release")
    parser.add_argument("--skip-clean-check", action="store_true",
                        help="allow a dirty working tree")
    options = parser.parse_args()

    version = (options.tag_override.lstrip("v") if options.tag_override else "") or current_version()
    tag = options.tag_override or f"v{version}"
    print(f"building release artefacts for {tag}\n")

    if not options.skip_clean_check:
        ensure_clean()

    artefacts: list[pathlib.Path] = []
    if options.with_bundle:
        if sys.platform == "win32":
            artefacts.append(build_bundle_zip(version))
        else:
            artefacts.append(build_linux_bundle_tarball(version))
    else:
        print("  (没有 --with-bundle，只写校验文件)")

    if options.with_drcomd or (not sys.platform == "win32" and options.with_bundle):
        daemon_tar = build_linux_daemon_tarball(version)
        if daemon_tar is not None:
            artefacts.append(daemon_tar)

    for existing in sorted(RELEASE_DIR.glob(f"*{version}*")):
        if existing.name in ("SHA256SUMS.txt", "NOTES.md") or "-dart" in existing.name:
            continue
        if existing.suffix in (".zip", ".gz", ".deb", ".rpm", ".zst") and existing not in artefacts:
            artefacts.append(existing)
    for inst in find_installers(version):
        if inst not in artefacts:
            artefacts.append(inst)
    if not artefacts:
        print("  (release/ 里没有该版本的安装包，只写校验文件)")
    write_checksums(artefacts, tag=tag, merge_existing=options.merge_checksums)

    print()
    if options.upload:
        for path in artefacts + [RELEASE_DIR / "SHA256SUMS.txt"]:
            print(f"uploading {path.name} ...")
            result = subprocess.run(
                ["gh", "release", "upload", tag, str(path), "--clobber"],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                print(f"  failed: {result.stderr.strip()}", file=sys.stderr)
                return 1
        print(f"\nuploaded. update the release notes if the asset list changed:")
        print(f"  gh release edit {tag} --notes-file release/NOTES.md")
    else:
        print("not uploading. to publish:")
        for path in artefacts + [RELEASE_DIR / "SHA256SUMS.txt"]:
            print(f"  gh release upload {tag} {path} --clobber")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
