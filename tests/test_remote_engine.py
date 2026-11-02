#!/usr/bin/env python3
"""Exercise RemoteEngine against a real drcomd + the mock server.

Proves the whole chain the GUI will use:
    RemoteEngine -> spawn drcomd.exe -> UDP to a fake server -> status polling

    python tests/test_remote_engine.py [--keep]

The daemon is pointed at a throwaway config in %TEMP% that talks to 127.0.0.1,
so nothing here can touch the real campus network or the real config file.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drcom.config import AppConfig, Account  # noqa: E402
from drcom.engine import EngineState  # noqa: E402
from drcom.logbus import LogBus  # noqa: E402
from drcom.remote_engine import RemoteEngine, daemon_path  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_config(path: Path, *, server_port: int, bind_port: int) -> AppConfig:
    """A config that authenticates against the loopback mock server only."""
    cfg = AppConfig()
    acc = Account(account="test-account", mac="AA:BB:CC:DD:EE:FF", password_protected="")
    cfg.accounts = [acc]
    cfg.active_account_id = acc.id
    cfg.auth.server = "127.0.0.1"
    cfg.auth.port = server_port
    cfg.auth.bind_port = bind_port
    cfg.auth.keepalive_interval = 0.8
    cfg.auth.timeout_ms = 700
    cfg.auth.post_login_delay = 0.05
    cfg.reconnect.min_delay = 0.3
    cfg.reconnect.max_delay = 1.0
    # The daemon decrypts password_protected; a plaintext tag would fail, so the
    # mock server accepts any password and we hand the daemon a DPAPI blob from
    # the user's own config instead.
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the temp config")
    args = parser.parse_args()

    exe = daemon_path()
    if exe is None:
        print("FAIL: drcomd.exe not built (run native\\build.bat)")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="drcom-remote-test-"))
    server_port = free_port()
    bind_port = free_port()
    api_port = free_port()

    # Use the real account/password from the user's config so the daemon can
    # actually decrypt something; only the network endpoints are changed.
    real_config = Path(os.path.expandvars(r"%APPDATA%\JLU-DrCOM-NG\config.json"))
    if real_config.is_file():
        data = json.loads(real_config.read_text(encoding="utf-8"))
    else:
        print("FAIL: no config.json to borrow credentials from")
        return 2
    data["auth"]["server"] = "127.0.0.1"
    data["auth"]["port"] = server_port
    data["auth"]["bind_port"] = bind_port
    data["auth"]["keepalive_interval"] = 0.8
    data["auth"]["timeout_ms"] = 700
    data["auth"]["post_login_delay"] = 0.05
    data["reconnect"]["min_delay"] = 0.3
    data["reconnect"]["max_delay"] = 1.0
    config_file = workdir / "config.json"
    config_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg = build_config(config_file, server_port=server_port, bind_port=bind_port)
    account = cfg.accounts[0]
    log = LogBus(log_dir=workdir / "logs", level="DEBUG")

    mock = subprocess.Popen(
        [sys.executable, str(ROOT / "native" / "tests" / "mock_server.py"),
         "--port", str(server_port), "--duration", "25"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)

    events: list[str] = []
    engine = RemoteEngine(
        config=cfg, account=account, password="unused", log=log,
        api_port=api_port, config_path=config_file,
    )
    engine.subscribe(lambda ev: events.append(ev.kind))

    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(f"[{' OK ' if ok else 'FAIL'}] {what}")
        if not ok:
            failures += 1

    print(f"daemon   : {exe}")
    print(f"api port : {api_port}   mock udp: {server_port}   bind: {bind_port}\n")

    try:
        engine.start()
        deadline = time.time() + 8
        while time.time() < deadline and not engine.is_online:
            time.sleep(0.2)

        check(engine.is_online, "RemoteEngine reached ONLINE via drcomd")
        check(engine.ip != "", f"IP reported by the daemon ({engine.ip})")
        check("online" in events, "an 'online' event reached the listener")

        # Let a couple of keepalives go by, then check uptime advances.
        time.sleep(2.0)
        check(engine.uptime_seconds() > 0.5, "uptime accrues while online")

        engine.stop()
        time.sleep(1.2)
        check(not engine.is_online, "stop() left ONLINE")
        check(engine.join(3.0), "polling thread joined")

        # The daemon must still be resident so the session outlives the GUI.
        with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/health", timeout=2) as r:
            health = json.loads(r.read())
        check("state" in health, "daemon still answers after logout")

    finally:
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{api_port}/shutdown", method="POST"),
                timeout=2,
            ).read()
        except Exception:
            pass
        mock.terminate()
        if not args.keep:
            time.sleep(0.5)
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"\nkept: {workdir}")

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
