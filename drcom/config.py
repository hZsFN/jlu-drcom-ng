"""Configuration model and persistence.

Stored as JSON (human-readable, diff-able, trivially inspectable).  The only
non-obvious part is the password field, which holds a Base64 body produced by
:mod:`drcom.secrets_store` — never the plaintext password.

Location resolution order:

1. ``--data-dir`` on the command line
2. ``<exe dir>/portable.txt`` marker present → keep everything next to the exe
3. ``%APPDATA%/JLU-DrCOM-NG`` on Windows, ``~/.config/jlu-drcom-ng`` elsewhere
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .secrets_store import ProtectError, protect, unprotect

__all__ = ["Account", "AppConfig", "ConfigStore", "default_data_dir"]

APP_NAME = "JLU-DrCOM-NG"
#: Lower-case form for POSIX config directories, where capitals are unusual.
APP_SLUG = "jlu-drcom-ng"
#: Data directory used before the project was renamed.  Migrated on first run
#: so an existing install keeps its saved credentials.
LEGACY_APP_NAME = "DrCOM-JLU"
CONFIG_FILENAME = "config.json"
KEY_FILENAME = "key.bin"

_DEFAULT_MAC_PLACEHOLDER = ""  # never ship a real MAC in a template


def default_data_dir() -> Path:
    """Where config, logs and stats live when the user has no preference."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "portable.txt").exists():
            return exe_dir / "data"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_SLUG


def legacy_data_dirs(primary: Path) -> list[Path]:
    """Data directories this project used before it was renamed."""
    candidates = [primary.parent / LEGACY_APP_NAME]
    if sys.platform != "win32":
        candidates.append(primary.parent / LEGACY_APP_NAME.lower())
    seen: list[Path] = []
    for candidate in candidates:
        if candidate != primary and candidate not in seen:
            seen.append(candidate)
    return seen


def migrate_legacy_data_dir(primary: Path) -> str:
    """Move a pre-rename data directory into place, once.

    Returns a human-readable note when something moved, otherwise an empty
    string.  Best-effort by design: failing to migrate must never stop the app
    from starting, it just means the user re-enters their password.
    """
    if primary.exists() and any(primary.iterdir()):
        return ""
    for legacy in legacy_data_dirs(primary):
        if not legacy.exists():
            continue
        try:
            primary.parent.mkdir(parents=True, exist_ok=True)
            if primary.exists():
                primary.rmdir()  # empty, created a moment ago
            legacy.rename(primary)
        except OSError:
            # Different volume, or a locked file: copy instead of moving.
            try:
                import shutil

                shutil.copytree(legacy, primary, dirs_exist_ok=True)
            except OSError:
                return ""
        return (
            f"已把旧数据目录 {legacy.name} 迁移为 {primary.name}，"
            "原有账号与加密密码继续可用。"
        )
    return ""


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------
@dataclass
class Account:
    """One set of credentials (P2: multiple accounts, switchable)."""

    account: str = ""
    mac: str = _DEFAULT_MAC_PLACEHOLDER
    password_protected: str = ""
    label: str = ""
    auto_login: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def display_name(self) -> str:
        return self.label or self.masked_account

    @property
    def masked_account(self) -> str:
        """Account with the middle redacted — safe for logs and screenshots."""
        text = self.account
        if len(text) <= 2:
            return "*" * len(text) if text else "(未设置)"
        if len(text) <= 5:
            return text[0] + "*" * (len(text) - 1)
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"

    def has_password(self) -> bool:
        return bool(self.password_protected)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
@dataclass
class AuthConfig:
    """Wire-level parameters. Defaults are the JLU campus network settings."""

    server: str = "10.100.61.3"
    port: int = 61440
    bind_address: str = "0.0.0.0"
    bind_port: int = 61440
    timeout_ms: int = 3000
    keepalive_interval: float = 20.0
    challenge_retries: int = 3
    post_login_delay: float = 0.2
    #: last-resort self-heal for the section 6.1 incident: if 61440 is
    #: system-reserved, try a nearby free source port instead.  Off by default
    #: because it deviates from the spec'd behaviour.
    allow_alternate_port: bool = False
    #: When bind_address is 0.0.0.0 and the challenge goes unanswered, retry
    #: bound to each concrete local address.  Fixes the very common "Wi-Fi or a
    #: VPN stole the default route so the campus server never answers" case.
    auto_interface_fallback: bool = True


@dataclass
class ReconnectConfig:
    """Throttling for the auto-reconnect loop (spec section 3.1)."""

    enabled: bool = True
    min_delay: float = 5.0
    max_delay: float = 300.0
    factor: float = 2.0
    jitter: float = 0.25
    #: stop hammering the server after this many consecutive failures and wait
    #: for the user (still retries slowly in the background)
    give_up_after: int = 8
    #: quiet-hours window during which reconnects are stretched out
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = "01:00"
    quiet_hours_end: str = "06:00"
    quiet_hours_delay: float = 300.0


#: What the window's X button does.  Three states rather than a bool, because
#: "remember my choice" needs a third answer that is also the default: ask.
CLOSE_ACTIONS = ("ask", "tray", "quit")

#: Human-readable names, shared by the settings selector and the toast.
CLOSE_ACTION_LABELS = {
    "ask": "每次询问",
    "tray": "最小化到托盘",
    "quit": "直接退出",
}


@dataclass
class UiConfig:
    minimize_to_tray: bool = True
    #: "ask" pops a chooser, "tray" hides to the tray, "quit" exits.
    close_action: str = "ask"
    start_minimized: bool = False
    autostart: bool = False
    auto_login_on_launch: bool = True
    notifications: bool = True
    #: honours the accessibility "reduce motion" preference (spec 3.4 rule 5)
    reduce_motion: bool = False
    high_contrast: bool = False
    window_width: int = 1100
    window_height: int = 780
    hud_decorations: bool = True
    #: The sweeping bright band.  Off by default: it is the only continuously
    #: animating element, it costs a websocket update ~13 times a second, and on
    #: a monitoring panel a moving highlight reads as flicker rather than as
    #: information.  The static fine scanlines stay on either way.
    scanline_animation: bool = False

    def __post_init__(self) -> None:
        # A hand-edited config could hold anything; an unknown action must not
        # leave the X button dead, so fall back to asking.
        if self.close_action not in CLOSE_ACTIONS:
            self.close_action = "ask"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    #: log every packet as hex — invaluable for protocol debugging
    protocol_hex: bool = True
    #: mask account numbers in log lines
    mask_accounts: bool = True
    #: DANGEROUS: dump the login packet verbatim (account + salted password
    #: digests included).  Only for byte-level protocol debugging.
    protocol_hex_unredacted: bool = False
    keep_days: int = 14
    max_file_mb: float = 8.0


@dataclass
class ApiConfig:
    """Local status surface so other programs can query this client (P2)."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8848
    status_file: bool = True
    status_file_path: str = ""


@dataclass
class NotifyConfig:
    desktop: bool = True
    #: POST the event as JSON to this URL (empty == disabled)
    webhook: str = ""
    #: run this command with ``{event}`` / ``{detail}`` substituted
    command: str = ""
    notify_on: list[str] = field(default_factory=lambda: ["online", "offline", "error", "login_failed"])


#: Probe targets as shipped.  The campus gateway is measured with ICMP; the
#: websites are measured with a real HTTP request, because on a machine running
#: a VPN in TUN mode ICMP does not reach the internet at all, and a TCP connect
#: only measures the local proxy (it answers in about a millisecond).
DEFAULT_PROBE_TARGETS = [
    "http://www.baidu.com",
    "http://www.bing.com",
    "10.100.61.3",
]

#: Values that only ever came from an older default.  Seeing one of these means
#: "never customised", so upgrading it is safe; anything else is a real user
#: choice and is left alone.
_LEGACY_PROBE_TARGETS = (
    ["10.100.61.3"],
    ["10.100.61.3", "10.10.10.10"],
)

#: Schema version of the stored config.  Bump it when a stored value has to be
#: rewritten once; _migrate() runs only while a file is older than this.
CONFIG_VERSION = 2


@dataclass
class ProbeConfig:
    """Network quality probing (P2)."""

    enabled: bool = True
    targets: list[str] = field(default_factory=lambda: list(DEFAULT_PROBE_TARGETS))
    interval: float = 30.0
    timeout_ms: int = 1500
    #: keep the game/streaming traffic untouched — probe only while offline
    only_when_offline: bool = False


@dataclass
class TrafficConfig:
    enabled: bool = True
    interval: float = 2.0


@dataclass
class ScheduleConfig:
    """Reconnect at fixed times (e.g. to pick up a new IP) — P2."""

    enabled: bool = False
    daily_times: list[str] = field(default_factory=lambda: [])
    #: also reconnect when the session has been up longer than this (0 = off)
    max_session_minutes: int = 0


# --------------------------------------------------------------------------
# Root config
# --------------------------------------------------------------------------
@dataclass
class AppConfig:
    version: int = 1
    accounts: list[Account] = field(default_factory=list)
    active_account_id: str = ""
    auth: AuthConfig = field(default_factory=AuthConfig)
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    # -- convenience ---------------------------------------------------
    def active_account(self) -> Account | None:
        for acc in self.accounts:
            if acc.id == self.active_account_id:
                return acc
        return self.accounts[0] if self.accounts else None

    def ensure_account(self) -> Account:
        """Return the active account, creating an empty one if needed."""
        acc = self.active_account()
        if acc is None:
            acc = Account()
            self.accounts.append(acc)
            self.active_account_id = acc.id
        return acc


# --------------------------------------------------------------------------
# (de)serialisation
# --------------------------------------------------------------------------
# NB: this module uses ``from __future__ import annotations``, so
# ``dataclasses.fields(...)[i].type`` is a *string* ("AuthConfig"), not the
# class.  Comparing that against ``__dataclass_fields__`` silently fails and
# nested sections would load as defaults — so resolve the real types first.
_TYPE_HINTS_CACHE: dict[type, dict[str, Any]] = {}



def _migrate(cfg: AppConfig) -> bool:
    """Rewrite values an older version got wrong.  Returns whether to save.

    Gated on the stored schema version rather than on a value comparison: a
    value test would fire on every launch, so a user who *deliberately* wanted
    campus-only targets would have that choice silently undone forever.
    """
    if cfg.version >= CONFIG_VERSION:
        return False

    if cfg.version < 2:
        # Up to 1.0.5 the shipped probe targets were campus addresses, so the
        # latency the dashboard showed was the campus link's ~1 ms rather than
        # anything to do with the internet.
        if cfg.probe.targets in _LEGACY_PROBE_TARGETS:
            cfg.probe.targets = list(DEFAULT_PROBE_TARGETS)

    cfg.version = CONFIG_VERSION
    return True

def _resolved_types(cls: type) -> dict[str, Any]:
    """Field name → real type object, with the annotations resolved."""
    cached = _TYPE_HINTS_CACHE.get(cls)
    if cached is not None:
        return cached
    import typing

    try:
        hints = typing.get_type_hints(cls)
    except Exception:  # pragma: no cover - unresolvable forward refs
        hints = {}
    _TYPE_HINTS_CACHE[cls] = hints
    return hints


def _nested_dataclasses(cls: type) -> dict[str, type]:
    """Fields of *cls* whose resolved type is itself a dataclass."""
    hints = _resolved_types(cls)
    out: dict[str, type] = {}
    for spec in fields(cls):
        hint = hints.get(spec.name)
        if hint is not None and hasattr(hint, "__dataclass_fields__"):
            out[spec.name] = hint
    return out


def _from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Build a dataclass from a dict, ignoring unknown keys.

    Tolerating unknown keys means a config written by a newer build does not
    brick an older one — it just drops what it does not understand.
    """
    known = {f.name for f in fields(cls)}
    nested = _nested_dataclasses(cls)
    kwargs: dict[str, Any] = {}
    for name in known:
        if name not in data:
            continue
        value = data[name]
        if name in nested and isinstance(value, dict):
            kwargs[name] = _from_dict(nested[name], value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


class ConfigStore:
    """Loads / saves :class:`AppConfig` and handles password (de)protection."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / CONFIG_FILENAME
        self.key_path = self.data_dir / KEY_FILENAME
        self.config = AppConfig()
        self._password_cache: dict[str, str] = {}
        self.load_error: str = ""

    # -- lifecycle -----------------------------------------------------
    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)

    def load(self) -> AppConfig:
        self.ensure_dirs()
        if not self.config_path.exists():
            self.config = AppConfig()
            self.config.ensure_account()
            return self.config
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Keep the broken file for forensics instead of silently nuking it.
            backup = self.config_path.with_suffix(".json.broken")
            try:
                self.config_path.replace(backup)
                self.load_error = f"配置文件损坏，已备份为 {backup.name}：{exc}"
            except OSError:
                self.load_error = f"配置文件损坏且无法备份：{exc}"
            self.config = AppConfig()
            self.config.ensure_account()
            return self.config

        cfg = AppConfig()
        cfg.version = int(raw.get("version", 1))

        accounts = []
        for item in raw.get("accounts", []) or []:
            if isinstance(item, dict):
                accounts.append(_from_dict(Account, item))
        cfg.accounts = accounts
        cfg.active_account_id = raw.get("active_account_id", "") or (
            accounts[0].id if accounts else ""
        )

        for name, cls in _nested_dataclasses(AppConfig).items():
            section = raw.get(name)
            if isinstance(section, dict):
                setattr(cfg, name, _from_dict(cls, section))

        # Always hand back something the UI can bind to.
        cfg.ensure_account()
        self.config = cfg

        # Rewrite what an older version got wrong, and remember that we did.
        if _migrate(cfg):
            try:
                self.save()
            except OSError:
                # A read-only config dir is not worth failing a launch over:
                # the migration is already applied in memory either way.
                pass
        return cfg

    def save(self) -> None:
        self.ensure_dirs()
        payload = asdict(self.config)
        # Write to a temp file then replace: a crash mid-write must not leave a
        # half-written config behind.
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.config_path)

    # -- password helpers ----------------------------------------------
    def set_password(self, account_id: str, password: str) -> bool:
        """Encrypt and store *password* for an account; returns degraded flag."""
        acc = self._find(account_id)
        if acc is None:
            return False
        result = protect(password, key_path=self.key_path)
        acc.password_protected = result.value
        self._password_cache[account_id] = password
        return result.degraded

    def get_password(self, account_id: str) -> str:
        """Return the plaintext password (in-memory only)."""
        if account_id in self._password_cache:
            return self._password_cache[account_id]
        acc = self._find(account_id)
        if acc is None or not acc.password_protected:
            return ""
        try:
            result = unprotect(acc.password_protected, key_path=self.key_path)
        except ProtectError:
            return ""
        self._password_cache[account_id] = result.value
        return result.value

    def password_backend_note(self, account_id: str) -> str:
        acc = self._find(account_id)
        if acc is None or not acc.password_protected:
            return ""
        try:
            return unprotect(acc.password_protected, key_path=self.key_path).warning
        except ProtectError as exc:
            return f"密码无法解密：{exc}"

    def clear_password_cache(self) -> None:
        self._password_cache.clear()

    # -- accounts -------------------------------------------------------
    def add_account(self, account: Account, *, make_active: bool = False) -> Account:
        self.config.accounts.append(account)
        if make_active or not self.config.active_account_id:
            self.config.active_account_id = account.id
        return account

    def find_by_account_name(self, account: str) -> Account | None:
        for acc in self.config.accounts:
            if acc.account == account:
                return acc
        return None

    def remove_account(self, account_id: str) -> bool:
        before = len(self.config.accounts)
        self.config.accounts = [a for a in self.config.accounts if a.id != account_id]
        self._password_cache.pop(account_id, None)
        if len(self.config.accounts) != before:
            if self.config.active_account_id == account_id:
                self.config.active_account_id = (
                    self.config.accounts[0].id if self.config.accounts else ""
                )
            return True
        return False

    def _find(self, account_id: str) -> Account | None:
        for acc in self.config.accounts:
            if acc.id == account_id:
                return acc
        return None
