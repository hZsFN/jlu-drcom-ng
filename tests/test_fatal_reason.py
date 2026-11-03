"""The daemon says why it stopped; the window used to show a bare "需处理".

These cover the translation layer in RemoteEngine._apply: a FATAL state must
arrive at the listener with the reason attached, and the reason must not be
silently dropped when the state itself is unchanged.
"""

from __future__ import annotations

import threading

from drcom.engine import EngineState
from drcom.remote_engine import REASON_ADVICE, REASON_TEXT, RemoteEngine


def _bare_engine():
    engine = object.__new__(RemoteEngine)
    engine.state = EngineState.IDLE
    engine.ip = ""
    engine.online_since = None
    engine.consecutive_failures = 0
    engine.last_reason = ""
    engine._announced_reason = ""
    engine._lock = threading.Lock()
    events = []
    engine._listeners = [events.append]
    return engine, events


def test_fatal_carries_the_daemon_reason():
    engine, events = _bare_engine()
    engine._apply({
        "state": "fatal", "online": False, "ip": "",
        "last_reason": "bind_failed", "consecutive_failures": 3,
    })
    assert [e.kind for e in events] == ["fatal"]
    assert events[0].message == REASON_TEXT["bind_failed"]
    assert events[0].advice == REASON_ADVICE["bind_failed"]
    assert engine.last_reason == "bind_failed"


def test_same_state_is_quiet_but_a_new_reason_is_announced():
    engine, events = _bare_engine()
    payload = {"state": "fatal", "online": False, "last_reason": "bind_failed"}
    engine._apply(dict(payload))
    events.clear()
    engine._apply(dict(payload, consecutive_failures=2))
    assert events == []
    engine._apply(dict(payload, last_reason="timeout"))
    assert [e.kind for e in events] == ["fatal"]
    assert events[0].message == REASON_TEXT["timeout"]


def test_unknown_reason_still_produces_a_message():
    engine, events = _bare_engine()
    engine._apply({"state": "fatal", "online": False, "last_reason": ""})
    assert events[0].message


def test_leaving_fatal_reannounces_next_time():
    engine, events = _bare_engine()
    engine._apply({"state": "fatal", "online": False, "last_reason": "timeout"})
    engine._apply({"state": "idle", "online": False, "last_reason": ""})
    events.clear()
    engine._apply({"state": "fatal", "online": False, "last_reason": "timeout"})
    assert [e.kind for e in events] == ["fatal"]
