"""Out-of-band notification channels (P2).

Three sinks, all optional and all failure-tolerant:

* a desktop toast (Windows 10/11 via the WinRT toast API, driven from
  PowerShell so we need no extra dependency);
* an HTTP webhook — POST a small JSON body, handy for Server 酱 / 飞书 / 钉钉
  style relays or a self-hosted logger;
* an arbitrary external command with ``{event}`` / ``{detail}`` placeholders.

Every sink runs on a daemon thread: a notification must never be able to slow
down or break the auth loop.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

__all__ = ["Notifier", "NotificationEvent"]

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class NotificationEvent:
    """What happened, in a form all three sinks can consume."""

    kind: str  # online / offline / login_failed / error / bind_failed
    title: str
    body: str
    detail: str = ""
    ip: str = ""
    error_code: int | None = None

    def as_dict(self) -> dict:
        return {
            "event": self.kind,
            "title": self.title,
            "body": self.body,
            "detail": self.detail,
            "ip": self.ip,
            "error_code": self.error_code,
        }


# PowerShell that raises a real Windows toast. Kept short; XML escaping is done
# on the Python side so the script itself stays fixed.
_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:DRCOM_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:DRCOM_TOAST_APPID).Show($toast)
"""


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


class Notifier:
    """Fan-out to the configured sinks."""

    def __init__(self, config, log) -> None:
        self.config = config
        self.log = log

    # -- public ---------------------------------------------------------
    def notify(self, event: NotificationEvent, *, force: bool = False) -> None:
        cfg = self.config
        if not force and cfg.notify_on and event.kind not in cfg.notify_on:
            return
        if cfg.desktop:
            self._spawn(self._toast, event)
        if cfg.webhook:
            self._spawn(self._webhook, event)
        if cfg.command:
            self._spawn(self._command, event)

    # -- sinks -----------------------------------------------------------
    def _spawn(self, func, event: NotificationEvent) -> None:
        thread = threading.Thread(target=self._safe, args=(func, event), daemon=True)
        thread.start()

    def _safe(self, func, event: NotificationEvent) -> None:
        try:
            func(event)
        except Exception as exc:  # pragma: no cover - notifications are best-effort
            self.log.debug(f"通知发送失败（{event.kind}）：{exc!r}")

    def _toast(self, event: NotificationEvent) -> None:
        if not IS_WINDOWS:
            self.log.debug(f"[通知] {event.title} — {event.body}")
            return
        xml = (
            "<toast><visual><binding template='ToastGeneric'>"
            f"<text>{_xml_escape(event.title)}</text>"
            f"<text>{_xml_escape(event.body)}</text>"
            "</binding></visual></toast>"
        )
        import os

        env = dict(os.environ)
        env["DRCOM_TOAST_XML"] = xml
        # PowerShell's own AppID already has a registered toast handler, so we
        # borrow it rather than installing a Start Menu shortcut of our own.
        env["DRCOM_TOAST_APPID"] = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS],
            capture_output=True,
            timeout=15,
            creationflags=0x08000000,
            env=env,
        )

    def _webhook(self, event: NotificationEvent) -> None:
        url = (self.config.webhook or "").strip()
        # Only http(s): urllib happily opens file://, ftp:// and friends, and the
        # webhook field is a free-text setting.
        if not url.lower().startswith(("http://", "https://")):
            self.log.warning("webhook 地址必须以 http:// 或 https:// 开头，已跳过：%r", url)
            return
        payload = json.dumps(event.as_dict(), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                self.log.debug(f"webhook 已投递，HTTP {response.status}")
        except urllib.error.URLError as exc:
            self.log.warning(f"webhook 投递失败：{exc}")

    def _command(self, event: NotificationEvent) -> None:
        template = self.config.command
        # Split FIRST, substitute SECOND.
        #
        # Substituting into the raw template and then tokenising let event data
        # change the argument structure: with a template like
        #   bash -c "notify-send \"{title}\""
        # a quote inside a server-supplied message could close the string early
        # and turn the rest of the message into separate argv entries.  Working
        # per token means substituted values can never re-tokenise anything.
        argv = shlex.split(template, posix=not IS_WINDOWS)
        if not argv:
            return
        replacements = {
            "{event}": event.kind,
            "{title}": event.title,
            "{body}": event.body,
            "{ip}": event.ip,
            "{detail}": event.detail,
        }
        rendered = []
        for token in argv:
            for placeholder, value in replacements.items():
                token = token.replace(placeholder, value)
            rendered.append(token)
        argv = rendered
        subprocess.run(
            argv,
            capture_output=True,
            timeout=20,
            creationflags=0x08000000 if IS_WINDOWS else 0,
        )
        self.log.debug(f"外部命令已执行：{argv[0]}")
