# DrCOM JLU

A campus-network authentication client for Jilin University, rewritten in
Python with a **HUD-styled** (heads-up display) interface built on Flet.

It does the same job as the official Qt client — challenge, login, keepalive —
but with an interface you can read, explanations instead of "unknown error", and
self-healing when the port is taken.

> 中文文档：[README_CN.md](README_CN.md)

| Offline | Online |
|---|---|
| ![idle](docs/screenshot-idle.png) | ![online](docs/screenshot-online.png) |

---

## What it does

**Authenticates** — the full Dr.COM flow (challenge → login → keepalive1 →
keepalive2, looping every 20 s), with automatic reconnect and exponential
backoff instead of hammering the server.

**Tells you what went wrong** — every login failure code is mapped to plain
language plus a next step. "Wrong password", not "error 0x03". "MAC not bound —
you may need to unbind in the self-service portal", not "error 0x0B".

**Fixes the port problem** — the client must bind UDP 61440, which is
frequently occupied. A bare `bind failed. Error code: 10013` is nearly useless,
because on Windows **10013 means two different things** and the code alone
cannot tell them apart: a system-reserved range (TUN/VPN adapters grab those) or
another program holding the port exclusively. So the client *measures* instead
of guessing — it scans neighbouring ports to find the blocked range, identifies
the process holding it, and gives advice that matches the actual cause.

**Survives a network that changes under it** — with Wi-Fi and Ethernet both up,
binding `0.0.0.0` succeeds but the kernel routes by metric, so the packet can
leave through the wrong adapter and the server never answers. The client retries
bound to each concrete local address until one works.

**Never stores your password in clear** — Windows DPAPI, bound to your account.

### Also included

- **Tray icon** and close-to-tray (needs `pystray`; without it, "close"
  minimises instead of hiding, so the window can never get lost)
- **Log panel** with byte-level protocol dumps, one-click export, masked accounts
- **Statistics** — today / this week / all-time uptime, disconnect count
- **Network probe** — latency, loss, jitter
- **Multiple accounts**, autostart, auto-login
- **Headless CLI** and a **local HTTP status API** for scripts and widgets
- **Traffic counters**, dark HUD theme, high-contrast mode, reduce-motion

---

## Getting started

### 1. Install

```bash
pip install -r requirements.txt
```

Windows is the primary target. Python 3.10 or newer.

### 2. Check the protocol implementation offline first

```bash
python main.py --selftest     # MD4 vectors, packet sizes, crypto backends
python main.py --doctor       # is port 61440 usable? if not, why?
```

Both are offline and safe. If `--doctor` reports a port problem it prints the
actual cause and what to do about it.

### 3. Configure

```bash
python main.py
```

On the **Account** page:

1. Enter your student ID
2. Click **Detect MAC** (or type it as `AA:BB:CC:DD:EE:FF`)
3. Enter your password
4. Save

The password is encrypted immediately and never lands on disk in clear text.

### 4. Connect

Back on the **Status** page, click **Log in**. The HUD shows the state, the
assigned IP, and how long you have been online. **Log out** stops the session.

---

## Command line

```bash
python main.py                          # graphical interface
python main.py --minimized              # start minimised (for autostart)
python main.py --cli login              # authenticate, stay in the foreground
python main.py --cli status --json      # query state (exit 0 = online, 1 = offline)
python main.py --cli diag               # who is holding port 61440?
python main.py --cli probe              # latency / packet loss
python main.py --cli export-logs        # dump the log buffer
python main.py --cli set --account 2023xxxxxxxx --mac AA:BB:CC:DD:EE:FF --password '***'
```

### Local status API

Off by default; enable it under *Settings → Local status API*.

```
GET  /status   /health   /metrics   /stats   /logs   /diag
POST /login    /logout   /reconnect /probe
```

It binds to `127.0.0.1` only. `/metrics` is Prometheus text format, so it drops
straight into a dashboard. A JSON status file is written alongside it.

---

## Building a standalone bundle

```bash
pip install pyinstaller
python build.py                 # folder build, all dependencies included
python build.py --onefile       # single .exe
```

The default output in `dist/` is self-contained: it runs on a machine with no
Python and no network, because the Flet desktop runtime is bundled inside it.
`python build.py --fetch-runtime` downloads that runtime first if you do not
already have it.

---

## Implementation notes

The wire protocol is a reimplementation of the one used by the original client,
and the packet layout was verified **byte for byte** against a recorded
successful login — all 373 bytes, plus `MD5A`, `MD5B`, `checksum1`, `checksum2`
and the MAC-XOR field.

Two details are worth flagging, because the widely-circulated notes about this
protocol get them wrong:

- `MD5B` hashes `0x01 + password + seed + 4 zero bytes`, not `0x01 + password +
  seed`.
- In `checksum2` the MAC sits at offset `counter+8`, after a 6-byte temp field.

More notes live in the module docstrings, and
`tools/verify_against_log.py` can re-run the byte comparison against your own
capture.

### Known limitations

- "Log out" is not a real logout packet — this Dr.COM variant has none. It stops
  the keepalives and closes the socket; the server releases the session on
  timeout. The UI says so rather than pretending otherwise.
- The tray needs `pystray`. Flet has no tray control, so without it the app
  minimises rather than hiding.
- The interface is dark-only, which is what a HUD needs. A high-contrast variant
  is available.
- Built and tested on Windows. The socket handling has POSIX paths, but they are
  less exercised.

---

## Credits

This project would not exist without
**[drcom-jlu-qt](https://github.com/code4lala/drcom-jlu-qt)** by
[code4lala](https://github.com/code4lala) — the Qt/C++ client that has been
authenticating JLU's campus network. It is the reference for the protocol: the
packet layout here was written by reading its source and diffing against the
captures it produces, and every field offset traces back to it.

Thanks also to [drcom-generic](https://github.com/drcoms/drcom-generic) and
[mchome/dogcom](https://github.com/mchome/dogcom), whose documentation of other
Dr.COM variants made the JLU-specific quirks much easier to spot.

### Contributors

- **大肥鱼** &lt;tanpan9926@mails.jlu.edu.cn&gt; — Python/Flet implementation
- **code4lala** — original Qt client and protocol reference

---

## License

GPL-3.0, the same license as the project this is derived from. See
[LICENSE](LICENSE).
