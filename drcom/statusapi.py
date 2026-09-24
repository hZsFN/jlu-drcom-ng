"""Local status API and status file (P2).

Lets another program — a wallpaper widget, a Stream Deck, a script, a home
dashboard — ask "am I online?".

* ``GET  /status``   full JSON snapshot
* ``GET  /health``   tiny ``{"online": true}`` for watchdogs
* ``GET  /metrics``  Prometheus-style plain text
* ``POST /login`` ``POST /logout`` ``POST /reconnect``  (localhost only)

Bound to ``127.0.0.1`` by default and never to a public interface unless the
user explicitly configures it.  The status file is rewritten atomically on every
state change so it is always self-consistent.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__all__ = ["StatusServer", "StatusWriter", "build_status_payload"]

MAX_BODY = 4096


def build_status_payload(controller) -> dict:
    """Assemble the JSON document served by ``/status``."""
    engine = controller.engine
    stats = controller.stats
    # Read the cached snapshot rather than sampling again: sample() derives the
    # rate from the delta since the previous call, so a second caller (the HTTP
    # request thread) would consume the ticker's window and make the displayed
    # rate alternate between the real value and zero.
    snapshot = controller.traffic.snapshot if controller.traffic else None

    # Every field is read defensively: this payload is a public surface (the
    # HTTP API and the status file), so a half-initialised engine must not be
    # able to make it raise.
    def engine_attr(name: str, default=None):
        return getattr(engine, name, default) if engine is not None else default

    state = engine_attr("state")
    reason = engine_attr("reason")
    uptime = 0.0
    if engine is not None:
        try:
            uptime = float(engine.uptime_seconds())
        except Exception:
            uptime = 0.0

    payload = {
        "app": "DrCOM-JLU",
        "version": controller.version,
        "state": getattr(state, "value", "idle"),
        "online": bool(engine is not None and getattr(engine, "is_online", False)),
        "ip": engine_attr("ip", "") or "",
        "account": controller.account.masked_account if controller.account else "",
        "server": controller.config.auth.server,
        "uptime_seconds": round(uptime, 1),
        "online_since": engine_attr("online_since"),
        "last_error": controller.last_error,
        "last_reason": getattr(reason, "value", "") or "",
        "consecutive_failures": engine_attr("consecutive_failures", 0) or 0,
        "ip_local": controller.local_ip,
        "mac": controller.account.mac if controller.account else "",
        "timestamp": time.time(),
    }
    if stats:
        payload["stats"] = stats.as_status()
    if snapshot:
        payload["traffic"] = {
            "interface": snapshot.interface,
            "rx_rate": round(snapshot.rx_rate, 1),
            "tx_rate": round(snapshot.tx_rate, 1),
            "session_rx": snapshot.total_rx,
            "session_tx": snapshot.total_tx,
        }
    if controller.probe:
        payload["quality"] = controller.probe.history.all_summaries()
    return payload


def render_metrics(payload: dict) -> str:
    """Flatten the status document into Prometheus text exposition format."""

    def emit(name: str, value, help_text: str, labels: str = "") -> str:
        label_part = f"{{{labels}}}" if labels else ""
        return f"# HELP {name} {help_text}\n# TYPE {name} gauge\n{name}{label_part} {value}\n"

    online = 1 if payload.get("online") else 0
    lines = [
        emit("drcom_online", online, "1 when the campus network session is online"),
        emit("drcom_uptime_seconds", payload.get("uptime_seconds", 0), "Current session uptime"),
        emit("drcom_consecutive_failures", payload.get("consecutive_failures", 0), "Consecutive failures"),
    ]
    stats = payload.get("stats") or {}
    for key in ("today_seconds", "today_drops", "today_sessions", "week_seconds", "total_seconds", "total_drops"):
        if key in stats:
            lines.append(emit(f"drcom_{key}", stats[key], f"DrCOM stat: {key}"))
    traffic = payload.get("traffic") or {}
    for key in ("rx_rate", "tx_rate", "session_rx", "session_tx"):
        if key in traffic:
            lines.append(emit(f"drcom_traffic_{key}", traffic[key], f"Traffic: {key}"))
    for item in payload.get("quality") or []:
        target = item.get("target", "")
        if item.get("rtt_ms") is not None:
            lines.append(
                emit("drcom_probe_rtt_ms", item["rtt_ms"], "Probe RTT", f'target="{target}"')
            )
        if item.get("loss_percent") is not None:
            lines.append(
                emit("drcom_probe_loss_percent", item["loss_percent"], "Probe loss", f'target="{target}"')
            )
    return "".join(lines)


@dataclass
class StatusWriter:
    """Writes the JSON status file atomically."""

    path: Path
    enabled: bool = True

    def write(self, payload: dict) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


class StatusServer:
    """The HTTP server; runs on a daemon thread."""

    def __init__(self, controller, *, host: str = "127.0.0.1", port: int = 8848, log=None) -> None:
        self.controller = controller
        self.host = host
        self.port = port
        self.log = log
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return self._server.server_address[1]

    def start(self) -> tuple[bool, str]:
        if self.is_running:
            return True, f"已在运行：http://{self.host}:{self.bound_port}/status"
        controller = self.controller

        class Handler(BaseHTTPRequestHandler):
            server_version = "DrCOM-JLU"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003 - silence default stderr spam
                if self.server and getattr(self.server, "verbose", False):
                    controller.log.debug("status api: " + fmt % args)

            # -- helpers --
            def _send(self, body: bytes, status: int = 200, content_type: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data: dict, status: int = 200) -> None:
                self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), status)

            def _local_only(self) -> bool:
                peer = self.client_address[0] if self.client_address else ""
                return peer in ("127.0.0.1", "::1", "localhost")

            # -- routes --
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                route = self.path.split("?", 1)[0].rstrip("/") or "/"
                try:
                    payload = build_status_payload(controller)
                except Exception as exc:
                    # A status endpoint that dies on a payload bug is worse than
                    # useless for monitoring, so always answer with something.
                    self._json({"error": f"status unavailable: {exc!r}"}, status=500)
                    return
                if route in ("/", "/status"):
                    self._json(payload)
                elif route == "/health":
                    self._json({"online": payload["online"], "state": payload["state"]})
                elif route == "/metrics":
                    self._send(render_metrics(payload).encode("utf-8"), content_type="text/plain")
                elif route == "/stats":
                    self._json(payload.get("stats", {}))
                elif route == "/logs":
                    entries = controller.log.snapshot()[-100:]
                    self._json({"lines": [f"{e.clock} {e.level} {e.message}" for e in entries]})
                elif route == "/diag":
                    self._json(controller.diagnostics())
                else:
                    self._json({"error": "unknown route", "routes": _ROUTES}, status=404)

            def do_POST(self) -> None:  # noqa: N802
                if not self._local_only():
                    self._json({"error": "control endpoints are localhost-only"}, status=403)
                    return
                route = self.path.split("?", 1)[0].rstrip("/")
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
                if length:
                    self.rfile.read(length)
                if route == "/login":
                    controller.request_login()
                    self._json({"ok": True, "action": "login"})
                elif route == "/logout":
                    controller.request_logout()
                    self._json({"ok": True, "action": "logout"})
                elif route == "/reconnect":
                    controller.request_reconnect()
                    self._json({"ok": True, "action": "reconnect"})
                elif route in ("/probe", "/probe/"):
                    results = controller.probe_now()
                    self._json({"ok": True, "results": [r.__dict__ for r in results]})
                else:
                    self._json({"error": "unknown route", "routes": _ROUTES}, status=404)

        _ROUTES = ["/status", "/health", "/metrics", "/stats", "/logs", "/diag", "/login", "/logout", "/reconnect", "/probe"]

        try:
            server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            return False, f"无法监听 {self.host}:{self.port} —— {exc}"
        server.daemon_threads = True
        server.verbose = False  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="drcom-api", daemon=True)
        self._thread.start()
        return True, f"状态接口已启动：http://{self.host}:{server.server_address[1]}/status"

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError:
                pass
            self._server = None
        self._thread = None
