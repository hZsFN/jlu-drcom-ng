#!/usr/bin/env python3
"""Build the Windows setup.exe for JLU DrCOM NG (Inno Setup 6).

    python tools/make_installer.py              # 编译安装包
    python tools/make_installer.py --open       # 编译完打开 release 目录
    python tools/make_installer.py --iscc PATH  # 指定 ISCC.exe

前置：dist/JLU-DrCOM-NG 已由 build.py 产出；Inno Setup 6 已安装
（默认装在 C:\\Program Files (x86)\\Inno Setup 6；也可用环境变量 ISCC 指定）。

产物：release/JLU-DrCOM-NG-<version>-setup.exe

版本号来源与 make_release.py 一致：先看 git tag，取不到再读 drcom/__init__.py。
这样安装包文件名、程序内部版本、release 资产名三处不会各说各话。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "dist" / "JLU-DrCOM-NG"
ISS = ROOT / "installer" / "installer.iss"
RELEASE_DIR = ROOT / "release"
PACKAGE_NAME = "JLU-DrCOM-NG"

ISCC_CANDIDATES = [
    pathlib.Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    pathlib.Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]


def find_iscc(explicit: str | None) -> pathlib.Path:
    if explicit:
        p = pathlib.Path(explicit)
        if not p.is_file():
            raise SystemExit(f"ISCC not found: {p}")
        return p
    env = os.environ.get("ISCC")
    if env and pathlib.Path(env).is_file():
        return pathlib.Path(env)
    for cand in ISCC_CANDIDATES:
        if cand.is_file():
            return cand
    raise SystemExit(
        "ISCC.exe not found. Install Inno Setup 6, or pass --iscc / set %ISCC%.\n"
        "winget install --id JRSoftware.InnoSetup -e   (needs network)"
    )


def tag_version() -> str | None:
    proc = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tag = proc.stdout.strip()
    return tag.lstrip("v") or None if proc.returncode == 0 else None


def source_version() -> str | None:
    init = ROOT / "drcom" / "__init__.py"
    if not init.is_file():
        return None
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def current_version() -> str:
    tv, sv = tag_version(), source_version()
    if tv and sv and tv != sv:
        print(f"warning: git tag says {tv}, drcom/__init__.py says {sv} — using git tag", file=sys.stderr)
    version = tv or sv
    if not version:
        raise SystemExit("cannot determine version: no git tag and no __version__ in drcom/__init__.py")
    return version


def check_dist() -> None:
    if not DIST_DIR.is_dir():
        raise SystemExit(f"missing {DIST_DIR} — run build.py first")
    exe = DIST_DIR / "JLU-DrCOM-NG.exe"
    if not exe.is_file():
        raise SystemExit(f"missing {exe} — run build.py first")
    files = sum(1 for _ in DIST_DIR.rglob("*") if _.is_file())
    size = sum(p.stat().st_size for p in DIST_DIR.rglob("*") if p.is_file())
    print(f"dist: {files} files, {size / 1048576:.1f} MB")


def build(version: str, iscc: pathlib.Path) -> pathlib.Path:
    RELEASE_DIR.mkdir(exist_ok=True)
    out = RELEASE_DIR / f"{PACKAGE_NAME}-{version}-setup.exe"
    if out.exists():
        out.unlink()
    cmd = [str(iscc), f"/DMyAppVersion={version}", str(ISS)]
    print("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    tail = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or not out.exists():
        print(tail[-4000:])
        raise SystemExit(f"ISCC failed (exit {proc.returncode})")
    for line in tail.splitlines():
        if "Compressed" in line or "successfully" in line.lower():
            print(line.strip())
    return out


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- 代码签名（可选）--------------------------------------------------------
# 没证书就跳过，并把「未签名」这件事明确打出来，别让人以为签过了。
# 有证书时用环境变量指过去，二选一：
#   SIGN_PFX=<D:\cert.pfx>  SIGN_PFX_PASSWORD=<口令>
#   SIGN_CERT_SHA1=<证书指纹>        （证书已装进 CurrentUser/My 或 LocalMachine/My）
# 可选 SIGN_TIMESTAMP_URL 换时间戳服务器（默认 DigiCert RFC3161）。

SIGNTOOL_CANDIDATES = pathlib.Path(r"C:\Program Files (x86)\Windows Kits\10\bin")


def find_signtool() -> pathlib.Path | None:
    env = os.environ.get("SIGNTOOL")
    if env and pathlib.Path(env).is_file():
        return pathlib.Path(env)
    if SIGNTOOL_CANDIDATES.is_dir():
        hits = sorted(SIGNTOOL_CANDIDATES.glob("*/x64/signtool.exe"), reverse=True)
        for hit in hits:
            if hit.is_file():
                return hit
    return None


def sign_config() -> dict | None:
    pfx = os.environ.get("SIGN_PFX")
    if pfx and pathlib.Path(pfx).is_file():
        return {"pfx": pfx, "password": os.environ.get("SIGN_PFX_PASSWORD", "")}
    thumb = os.environ.get("SIGN_CERT_SHA1")
    if thumb:
        return {"thumbprint": thumb}
    return None


def sign_file(path: pathlib.Path, cfg: dict, signtool: pathlib.Path) -> bool:
    ts = os.environ.get("SIGN_TIMESTAMP_URL", "http://timestamp.digicert.com")
    cmd = [str(signtool), "sign", "/fd", "sha256", "/td", "sha256", "/tr", ts,
           "/d", "JLU DrCOM NG"]
    if "pfx" in cfg:
        cmd += ["/f", cfg["pfx"]]
        if cfg.get("password"):
            cmd += ["/p", cfg["password"]]
    else:
        cmd += ["/sha1", cfg["thumbprint"]]
    cmd.append(str(path))
    print("$ " + " ".join(cmd[:-1] + ["<setup.exe>"]))
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        print(tail[-2000:])
        print(f"warning: signing failed (exit {proc.returncode}) — shipping an unsigned installer",
              file=sys.stderr)
        return False
    print("signed: " + (tail.splitlines()[-1].strip() if tail else "ok"))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="build the Windows setup.exe")
    ap.add_argument("--iscc", help="path to ISCC.exe")
    ap.add_argument("--open", action="store_true", help="open release/ afterwards")
    ap.add_argument("--no-sign", action="store_true", help="skip code signing even if a cert is configured")
    args = ap.parse_args()

    iscc = find_iscc(args.iscc)
    print(f"ISCC: {iscc}")
    version = current_version()
    print(f"version: {version}")
    check_dist()

    out = build(version, iscc)

    cfg = None if args.no_sign else sign_config()
    signtool = find_signtool()
    if cfg and signtool:
        sign_file(out, cfg, signtool)
    elif cfg and not signtool:
        print("warning: a signing cert is configured but signtool.exe was not found — "
              "install the Windows SDK or set %SIGNTOOL%", file=sys.stderr)
    else:
        print("unsigned: no code-signing cert configured "
              "(set SIGN_PFX + SIGN_PFX_PASSWORD, or SIGN_CERT_SHA1).\n"
              "          SmartScreen will show \"Windows protected your PC\" until the "
              "installer is signed and builds reputation.")

    size = out.stat().st_size
    print(f"\n{out.relative_to(ROOT)}")
    print(f"  {size / 1048576:.1f} MB")
    print(f"  sha256 {sha256_of(out)}")

    if args.open:
        subprocess.run(["explorer", str(RELEASE_DIR)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
