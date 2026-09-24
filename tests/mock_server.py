"""A minimal Dr.COM server so the whole state machine can be tested offline.

It speaks just enough of the protocol to drive the client through
challenge → login → keepalive1 → keepalive2, and it can be told to fail in
specific ways (wrong password, server busy, stop answering keepalives) so the
reconnect logic can be exercised without touching the campus network.

It also *validates* what the client sends: packet lengths, the checksum fields
and the MAC placement.  If our builder regresses, the server notices.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field

from drcom import protocol


@dataclass
class ServerBehaviour:
    """Knobs the tests use to steer the fake server."""

    #: return 0x04 normally, or a failure code (e.g. 3 == wrong password)
    login_error_code: int | None = None
    #: stop answering keepalive1 after N successful cycles (None == never)
    die_after_cycles: int | None = None
    #: answer keepalive2 with a bad opcode
    break_keepalive2: bool = False
    #: send a server notice (0x4D) before the keepalive1 reply, once
    send_notice_once: bool = True
    #: delay before answering the challenge, to exercise timeouts
    challenge_delay: float = 0.0


@dataclass
class ServerLog:
    """What the client actually sent, for assertions."""

    challenges: list[bytes] = field(default_factory=list)
    logins: list[bytes] = field(default_factory=list)
    keepalive1: list[bytes] = field(default_factory=list)
    keepalive2: list[bytes] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


class MockAuthServer:
    """Threaded UDP server implementing the JLU Dr.COM exchange."""

    def __init__(self, behaviour: ServerBehaviour | None = None) -> None:
        self.behaviour = behaviour or ServerBehaviour()
        self.log = ServerLog()
        self.seed = bytes((0x21, 0x79, 0x04, 0x0F))
        self.auth_information = bytes(range(0x40, 0x50))
        # RFC 5737 documentation address - never a real machine.
        self.client_ip = "192.0.2.10"
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="mock-drcom", daemon=True)
        self.cycles = 0
        self._notice_sent = False
        self._keepalive_counter = 0

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "MockAuthServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2.0)
        self._sock.close()

    def __enter__(self) -> "MockAuthServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- helpers ----------------------------------------------------------
    def _challenge_reply(self) -> bytes:
        reply = bytearray(76)
        reply[0] = 0x02
        reply[1] = 0x02
        reply[2:4] = self.seed[2:4]
        reply[4:8] = self.seed
        reply[20:24] = bytes(int(part) for part in self.client_ip.split("."))
        return bytes(reply)

    def _login_reply(self) -> bytes:
        reply = bytearray(100)
        if self.behaviour.login_error_code is None:
            reply[0] = 0x04
            reply[23:39] = self.auth_information
        else:
            reply[0] = 0x05
            reply[4] = self.behaviour.login_error_code
        return bytes(reply)

    def _validate_login(self, packet: bytes) -> None:
        """Check the structural invariants of whatever the client sent."""
        if len(packet) < protocol.LOGIN_BASE_LEN:
            self.log.problems.append(f"login packet too short: {len(packet)}")
            return
        if packet[:3] != bytes((0x03, 0x01, 0x00)):
            self.log.problems.append(f"bad login header: {packet[:3].hex()}")
        account_length = packet[3] - 20
        password_length = packet[313]
        counter = 312 + 2 + password_length
        expected_length = protocol.LOGIN_BASE_LEN + protocol._password_padding(password_length)[0]
        if len(packet) != expected_length:
            self.log.problems.append(
                f"login length {len(packet)} != expected {expected_length} for pw len {password_length}"
            )
        if packet[97:105] != protocol.compute_checksum1(packet):
            self.log.problems.append("checksum1 mismatch")
        mac = packet[counter + 8 : counter + 14]
        if packet[counter + 2 : counter + 6] != protocol.compute_checksum2(
            packet[: counter + 2], mac
        ):
            self.log.problems.append("checksum2 mismatch")
        if len(packet) < counter + 16:
            self.log.problems.append("login packet truncated after the password section")
        if account_length <= 0:
            self.log.problems.append("empty account in login packet")

    # -- main loop ---------------------------------------------------------
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._handle(data, peer)
            except Exception as exc:  # pragma: no cover - surfaced via problems
                self.log.problems.append(f"server error: {exc!r}")

    def _handle(self, data: bytes, peer) -> None:
        if not data:
            return

        # --- challenge: 20 bytes, opcode 0x01 -----------------------------
        if len(data) == 20 and data[0] == 0x01:
            self.log.challenges.append(data)
            if self.behaviour.challenge_delay:
                time.sleep(self.behaviour.challenge_delay)
            self._sock.sendto(self._challenge_reply(), peer)
            return

        # --- login: opcode 0x03 -------------------------------------------
        if data[0] == 0x03:
            self.log.logins.append(data)
            self._validate_login(data)
            self._sock.sendto(self._login_reply(), peer)
            return

        # --- keepalive-1 first packet: 8 bytes ----------------------------
        if len(data) == 8 and data[0] == 0x07:
            self.log.keepalive1.append(data)
            self.cycles += 1
            if self.behaviour.die_after_cycles is not None and self.cycles > self.behaviour.die_after_cycles:
                return  # go silent → client must time out and reconnect
            if self.behaviour.send_notice_once and not self._notice_sent:
                self._notice_sent = True
                notice = bytearray(24)
                notice[0] = 0x4D
                self._sock.sendto(bytes(notice), peer)
            reply = bytearray(20)
            reply[0] = 0x07
            reply[8:12] = self.seed
            self._sock.sendto(bytes(reply), peer)
            return

        # --- keepalive-1 second packet: 42 bytes --------------------------
        if len(data) == protocol.KEEPALIVE1_PACKET2_LEN:
            self.log.keepalive1.append(data)
            if data[0] != 0xFF:
                self.log.problems.append(f"keepalive1 p2 opcode {data[0]:#x} != 0xff")
            if data[8:12] != self.seed:
                self.log.problems.append("keepalive1 p2 seed mismatch")
            expected_crc = protocol.gen_crc(self.seed, self.seed[0] & 3)
            if data[12:20] != expected_crc:
                self.log.problems.append("keepalive1 p2 CRC mismatch")
            if data[20:36] != self.auth_information:
                self.log.problems.append("keepalive1 p2 auth_information mismatch")
            reply = bytearray(20)
            reply[0] = 0x07
            self._sock.sendto(bytes(reply), peer)
            return

        # --- keepalive-2: 40 bytes ----------------------------------------
        if len(data) == protocol.KEEPALIVE2_LEN and data[0] == 0x07:
            self.log.keepalive2.append(data)
            file_packet = data[6:8] == bytes((0x0F, 0x27))
            reply = bytearray(40)
            reply[0] = 0x07
            reply[1] = data[1]
            if self.behaviour.break_keepalive2:
                reply[2] = 0x29
            elif file_packet:
                reply[2] = 0x10
            else:
                reply[2] = 0x28
                reply[16:20] = bytes((0xCA, 0xFE, 0xBA, 0xBE))
            self._sock.sendto(bytes(reply), peer)
            return

        self.log.problems.append(f"unexpected packet: len={len(data)} first={data[0]:#x}")


def free_udp_port() -> int:
    """A currently-free local port, for use as ``bind_port`` in tests."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
