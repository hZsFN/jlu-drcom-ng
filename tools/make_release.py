#!/usr/bin/env python3
"""Build the release artefacts for JLU DrCOM NG.

Keeps the naming in one place.  The three artefacts drifted twice during the
first release (source-only, then a missing executable, then a renamed source
archive), which is what this exists to prevent.

    python tools/make_release.py                 # source zip + checksums
    python tools/make_release.py --with-bundle   # + the Windows bundle (needs dist/)
    python tools/make_release.py --upload        # build, then hand over to gh

Artefact names (keep the two archives parallel):

    JLU-DrCOM-NG-<version>-source-code.zip     git archive of the tag
    JLU-DrCOM-NG-<version>-windows-x64.zip     dist/ bundle, Python + Flet inside
    SHA256SUMS.txt                             digests of both

Uploading is deliberately *not* automatic unless ``--upload`` is passed: this
writes files and prints the exact command, so a human sees what is going out.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / "release"
DIST_DIR = ROOT / "dist" / "JLU-DrCOM-NG"
PACKAGE_NAME = "JLU-DrCOM-NG"


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def current_version() -> str:
    """The version from the newest tag, e.g. ``v1.0.0`` -> ``1.0.0``."""
    tag = run(["git", "describe", "--tags", "--abbrev=0"]).stdout.strip()
    if not tag:
        raise SystemExit("no git tag found; tag the release commit first")
    return tag.lstrip("v")


def ensure_clean() -> None:
    status = run(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        print("working tree is not clean:\n" + status, file=sys.stderr)
        print("\ncommit or stash first — the source archive is built from the tag,"
              " but a dirty tree usually means the tag is behind.", file=sys.stderr)
        raise SystemExit(1)


def build_source_zip(version: str) -> pathlib.Path:
    """Source archive, laid out the way GitHub's "Download ZIP" is."""
    tag = f"v{version}"
    prefix = f"jlu-drcom-ng-{version}/"          # lowercase, matching the repo name
    target = RELEASE_DIR / f"{PACKAGE_NAME}-{version}-source-code.zip"
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    run(["git", "archive", "--format=zip", f"--prefix={prefix}", "-o", str(target), tag])
    with zipfile.ZipFile(target) as archive:
        count = len([n for n in archive.namelist() if not n.endswith("/")])
    print(f"  source  {target.name}  ({count} files, {target.stat().st_size / 1024:.0f} KB)")
    return target


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


def write_checksums(paths: list[pathlib.Path]) -> pathlib.Path:
    target = RELEASE_DIR / "SHA256SUMS.txt"
    lines = []
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    target.write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"  hashes  {target.name}")
    for line in lines:
        print(f"            {line}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-bundle", action="store_true",
                        help="also zip dist/ (requires a completed build)")
    parser.add_argument("--upload", action="store_true",
                        help="upload the artefacts to the GitHub release")
    parser.add_argument("--skip-clean-check", action="store_true",
                        help="allow a dirty working tree")
    options = parser.parse_args()

    version = current_version()
    tag = f"v{version}"
    print(f"building release artefacts for {tag}\n")

    if not options.skip_clean_check:
        ensure_clean()

    artefacts = [build_source_zip(version)]
    if options.with_bundle:
        artefacts.append(build_bundle_zip(version))
    write_checksums(artefacts)

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
