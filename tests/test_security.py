"""Regression tests for the security-audit hardening pass.

Every test here corresponds to a finding that held up against the actual code.
The findings that did *not* hold up are reported to the user rather than encoded
as tests, so this file stays a record of real defects.
"""

from __future__ import annotations

import json
import socket

import pytest


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def http_request(port: int, method: str, path: str, *, headers: dict | None = None,
                 body: bytes = b"", host_header: str | None = None) -> tuple[int, bytes]:
    """Send a raw HTTP request so headers can be controlled precisely."""
    lines = [f"{method} {path} HTTP/1.1"]
    lines.append(f"Host: {host_header}" if host_header is not None else f"Host: 127.0.0.1:{port}")
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        chunks: list[bytes] = []
        sock.settimeout(3)
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
    raw = b"".join(chunks)
    status = int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP/") else 0
    _, _, payload = raw.partition(b"\r\n\r\n")
    return status, payload


class _FakeProc:
    stdout = ""
    returncode = 0


@pytest.fixture
def api(controller):
    ok, message = controller.api.start()
    if not ok:
        pytest.skip(f"cannot bind the status port here: {message}")
    yield controller.api
    controller.api.stop()


# --------------------------------------------------------------------------
# MEDIUM-1/2: control endpoints used to accept any local request
# --------------------------------------------------------------------------
def test_control_endpoint_rejects_a_cross_site_post(api) -> None:
    """The audit's own PoC: a page in the user's browser POSTs to localhost.

    The peer address *is* 127.0.0.1, because the browser opens the connection --
    which is exactly why a peer-IP-only check is not enough.  The Origin is now
    refused.
    """
    status, payload = http_request(
        api.bound_port, "POST", "/logout",
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403
    assert b"cross-site" in payload


def test_control_endpoint_requires_a_token(api) -> None:
    status, payload = http_request(api.bound_port, "POST", "/logout")
    assert status == 403
    assert b"X-DrCOM-Token" in payload


def test_control_endpoint_accepts_the_real_token(api) -> None:
    status, payload = http_request(
        api.bound_port, "POST", "/logout",
        headers={"X-DrCOM-Token": api.control_token},
    )
    assert status == 200
    assert json.loads(payload)["ok"] is True


def test_control_endpoint_rejects_a_wrong_token(api) -> None:
    status, _ = http_request(
        api.bound_port, "POST", "/reconnect",
        headers={"X-DrCOM-Token": "not-the-token"},
    )
    assert status == 403


def test_control_endpoint_rejects_a_rebinding_host_header(api) -> None:
    """DNS rebinding: Host names the attacker even though the IP is ours."""
    status, payload = http_request(
        api.bound_port, "POST", "/logout",
        headers={"X-DrCOM-Token": api.control_token},
        host_header="evil.example",
    )
    assert status == 403
    assert b"Host" in payload


def test_get_endpoints_refuse_a_rebinding_host_header(api) -> None:
    """Read endpoints stay open, but must still be addressed to this machine."""
    ok, _ = http_request(api.bound_port, "GET", "/status")
    assert ok == 200
    blocked, _ = http_request(api.bound_port, "GET", "/status", host_header="evil.example")
    assert blocked == 403


def test_preflight_is_never_granted(api) -> None:
    """No CORS headers, so a cross-origin request cannot carry the token."""
    status, _ = http_request(api.bound_port, "OPTIONS", "/logout")
    assert status == 405


def test_token_is_written_to_the_data_directory(controller) -> None:
    ok, message = controller.api.start()
    if not ok:
        pytest.skip(message)
    try:
        token_file = controller.data_dir / "api-token.txt"
        assert token_file.exists()
        assert token_file.read_text(encoding="utf-8").strip() == controller.api.control_token
    finally:
        controller.api.stop()


def test_token_is_fresh_per_server_instance(controller) -> None:
    """A new secret each run, not a constant baked into the source."""
    from drcom.statusapi import StatusServer

    other = StatusServer(controller, host="127.0.0.1", port=0)
    assert other.control_token != controller.api.control_token
    assert len(other.control_token) >= 20


# --------------------------------------------------------------------------
# HIGH-2: malformed Content-Length used to raise inside the handler
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["abc", "", "-5", "99999999999999999999", "1e5", "  "])
def test_malformed_content_length_does_not_crash(api, value: str) -> None:
    status, _ = http_request(
        api.bound_port, "POST", "/logout",
        headers={"X-DrCOM-Token": api.control_token, "Content-Length": value},
    )
    assert status != 500, f"Content-Length={value!r} produced a server error"
    assert status in (200, 400, 403, 413)


def test_content_length_is_capped() -> None:
    from drcom.statusapi import MAX_BODY, _parse_content_length

    assert _parse_content_length("999999999") == MAX_BODY
    assert _parse_content_length(None) == 0
    assert _parse_content_length("junk") == 0
    assert _parse_content_length("-1") == 0
    assert _parse_content_length("12") == 12


# --------------------------------------------------------------------------
# LOW-1: Prometheus label escaping
# --------------------------------------------------------------------------
def test_prometheus_label_escaping() -> None:
    from drcom.statusapi import _prometheus_escape

    assert _prometheus_escape('a"b') == 'a\\"b'
    assert _prometheus_escape("a\\b") == "a\\\\b"
    assert _prometheus_escape("a\nb") == "a\\nb"


def test_metrics_survive_a_hostile_target(controller) -> None:
    """A quote in a probe target must not inject fake metrics."""
    from drcom.netprobe import PingResult
    from drcom.statusapi import build_status_payload, render_metrics

    controller.probe.history.add(PingResult('x"} drcom_injected 1\n#', 1, 1, 1.0))
    text = render_metrics(build_status_payload(controller))
    assert "# TYPE drcom_injected" not in text
    assert '\\"' in text


# --------------------------------------------------------------------------
# LOW-2: ping argument injection
# --------------------------------------------------------------------------
@pytest.mark.parametrize("target", ["-f", "--flood", "-i", "-c1000", "-"])
def test_ping_refuses_option_like_targets(target: str, monkeypatch) -> None:
    from drcom import netprobe

    called: list[list[str]] = []
    monkeypatch.setattr(
        netprobe.subprocess, "run",
        lambda argv, **kw: called.append(argv) or _FakeProc(),
    )
    result = netprobe.ping_once(target, count=1)
    assert called == [], f"ping was invoked with {target!r}: {called}"
    assert not result.ok


@pytest.mark.parametrize("target", ["10.100.61.3", "example.com", "172.18.123.63"])
def test_ping_accepts_normal_targets(target: str, monkeypatch) -> None:
    from drcom import netprobe

    called: list[list[str]] = []
    monkeypatch.setattr(
        netprobe.subprocess, "run",
        lambda argv, **kw: called.append(argv) or _FakeProc(),
    )
    netprobe.ping_once(target, count=1)
    assert called and target in called[0]


def test_ping_terminates_options_on_posix(monkeypatch) -> None:
    """``--`` stops a target from ever being read as a flag."""
    from drcom import netprobe

    monkeypatch.setattr(netprobe, "IS_WINDOWS", False)
    called: list[list[str]] = []
    monkeypatch.setattr(
        netprobe.subprocess, "run",
        lambda argv, **kw: called.append(argv) or _FakeProc(),
    )
    netprobe.ping_once("10.0.0.1", count=1)
    assert "--" in called[0]
    assert called[0].index("--") < called[0].index("10.0.0.1")


def test_probe_ignores_hostile_targets_end_to_end(controller) -> None:
    controller.config.probe.targets = ["-f", "--flood"]
    results = controller.probe.probe_now()
    assert results and all(not r.ok for r in results)


# --------------------------------------------------------------------------
# LOW-3: webhook scheme
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x", "not a url"]
)
def test_webhook_rejects_non_http_schemes(controller, monkeypatch, url: str) -> None:
    from drcom.notify import NotificationEvent, Notifier
    import drcom.notify as notify_module

    opened: list[str] = []

    def spy(request, *args, **kwargs):
        opened.append(str(request))
        raise AssertionError("urlopen should not have been reached")

    monkeypatch.setattr(notify_module.urllib.request, "urlopen", spy)
    notifier = Notifier(controller.config.notify, controller.log)
    notifier.config.webhook = url
    notifier._webhook(NotificationEvent(kind="online", title="t", body="b"))
    assert opened == [], f"opened {url!r}"


# --------------------------------------------------------------------------
# LOW-4: notify.command tokenisation
# --------------------------------------------------------------------------
def test_command_template_substitution_cannot_retokenise(controller, monkeypatch) -> None:
    """The audit's PoC: a quote in a server-supplied message must not add argv
    entries.  ``{title}`` is substituted *after* the template is split, so a
    quote inside it stays inside that one argument."""
    from drcom.notify import NotificationEvent, Notifier
    import drcom.notify as notify_module

    ran: list[list[str]] = []
    monkeypatch.setattr(
        notify_module.subprocess, "run",
        lambda argv, **kw: ran.append(list(argv)) or _FakeProc(),
    )
    notifier = Notifier(controller.config.notify, controller.log)
    notifier.config.command = 'notify-send "{title}"'
    hostile = 'x"; touch /tmp/pwned; echo "'
    notifier._command(NotificationEvent(kind="error", title=hostile, body="b"))

    assert len(ran) == 1
    argv = ran[0]
    assert len(argv) == 2, f"the payload changed the argument structure: {argv}"
    assert argv[0] == "notify-send"
    assert hostile in argv[1]


def test_command_template_still_substitutes_normally(controller, monkeypatch) -> None:
    from drcom.notify import NotificationEvent, Notifier
    import drcom.notify as notify_module

    ran: list[list[str]] = []
    monkeypatch.setattr(
        notify_module.subprocess, "run",
        lambda argv, **kw: ran.append(list(argv)) or _FakeProc(),
    )
    notifier = Notifier(controller.config.notify, controller.log)
    notifier.config.command = "notify.bat {event} {ip}"
    notifier._command(NotificationEvent(kind="online", title="t", body="b", ip="10.0.0.1"))
    assert ran and ran[0][-2:] == ["online", "10.0.0.1"]


# --------------------------------------------------------------------------
# HIGH-1: pid taken from netstat output
# --------------------------------------------------------------------------
def test_port_holder_lookup_ignores_non_numeric_pids(monkeypatch) -> None:
    from drcom import binding

    calls: list[list[str]] = []

    def fake_run_hidden(cmd, timeout=6.0):
        calls.append(cmd)
        if cmd[0] == "netstat":
            return (
                "  UDP    0.0.0.0:61440    *:*    1234\n"
                "  UDP    0.0.0.0:61440    *:*    not-a-pid\n"
            )
        return "x.exe"

    monkeypatch.setattr(binding, "IS_WINDOWS", True)
    monkeypatch.setattr(binding, "_run_hidden", fake_run_hidden)

    holders = binding.find_port_holders(61440)
    tasklist_calls = " ".join(" ".join(c) for c in calls if c[0] == "tasklist")
    assert "not-a-pid" not in tasklist_calls, "a non-numeric pid reached tasklist"
    assert "1234" in tasklist_calls
    assert holders


# --------------------------------------------------------------------------
# the dead helper the audit flagged should be gone
# --------------------------------------------------------------------------
def test_netiface_has_no_unused_run_helper() -> None:
    import drcom.netiface as netiface

    assert not hasattr(netiface, "_run"), "the unused _run helper should be removed"
