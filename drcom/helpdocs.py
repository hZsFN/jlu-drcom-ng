"""Where the troubleshooting guide lives, and how to reach it.

The guide is written for the machine that is failing, so it has to be readable
with no network: the shipped copy wins, the repository copy is the fallback for
a source checkout, and the GitHub page is the last resort (a machine whose
problem *is* the network cannot fetch it).
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["GUIDE_NAME", "GUIDE_URL", "find_guide", "read_guide"]

GUIDE_NAME = "troubleshooting.md"
GUIDE_URL = f"https://github.com/hZsFN/jlu-drcom-ng/blob/main/docs/{GUIDE_NAME}"


def _candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    out: list[Path] = []
    if getattr(sys, "frozen", False):
        # A packaged build: the installer drops docs/ next to the executable, and
        # PyInstaller's --add-data puts its copy inside the bundle root, which is
        # sys._MEIPASS (for a one-folder build that is _internal/).  Both are
        # checked because either path is a legitimate packaging choice.
        exe_dir = Path(sys.executable).resolve().parent
        bundle_root = getattr(sys, "_MEIPASS", None)
        for base in (exe_dir, Path(bundle_root) if bundle_root else None):
            if base is None:
                continue
            out.append(base / "docs" / GUIDE_NAME)
            out.append(base / GUIDE_NAME)
    out.append(here.parent / "docs" / GUIDE_NAME)
    out.append(Path.cwd() / "docs" / GUIDE_NAME)
    return out


def find_guide() -> Path | None:
    """The first readable copy of the guide, or None when none is installed."""
    for path in _candidates():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def read_guide() -> str:
    """The guide's text, or "" when no copy is available."""
    path = find_guide()
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
