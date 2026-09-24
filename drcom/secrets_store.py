"""Password-at-rest protection.

Strategy, best available first:

1. **Windows DPAPI** (``CryptProtectData`` / ``CryptUnprotectData`` via ctypes).
   The ciphertext is bound to the *current user's* login credentials, so a
   copied config file is useless on another machine or account.  No extra
   dependency, no master password to store.  This is what the reference Qt
   client does and it is a genuinely good fit here.
2. **``cryptography`` Fernet** on non-Windows, when the package is importable.
   The key lives in a sibling file with ``0600`` permissions.
3. **Obfuscation only**, as a clearly-labelled last resort.  Never silent: the
   caller gets :attr:`ProtectionResult.degraded` and the UI warns the user.

The plaintext password is never logged, never written to disk in the clear,
and never placed in an exception message.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProtectionResult",
    "decrypt_dpapi",
    "protect",
    "unprotect",
    "protection_backend_name",
]

IS_WINDOWS = sys.platform == "win32"

#: Marker byte prefixed to the stored blob, so we can tell which backend
#: produced it and refuse to feed a Fernet token to DPAPI.
_TAG_DPAPI = b"D1:"
_TAG_FERNET = b"F1:"
_TAG_OBFUSCATED = b"X1:"


class ProtectError(Exception):
    """Raised when a stored secret cannot be recovered."""


@dataclass(frozen=True)
class ProtectionResult:
    """A protected (or unprotected) secret plus how it was produced."""

    value: str
    backend: str
    degraded: bool = False

    @property
    def warning(self) -> str:
        if not self.degraded:
            return ""
        return (
            f"密码以「{self.backend}」方式保存，仅做了可逆混淆，"
            "任何能读到配置文件的人都能还原。建议在 Windows 下运行以启用 DPAPI 加密。"
        )


# --------------------------------------------------------------------------
# Windows DPAPI
# --------------------------------------------------------------------------
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_to(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_call(func, data: bytes, description: str | None = None) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    fn = getattr(crypt32, func)
    fn.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    fn.restype = wintypes.BOOL

    blob_in = _blob_from(data)
    blob_out = _DataBlob()
    ok = fn(
        ctypes.byref(blob_in),
        description,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ProtectError(f"{func} 失败，Win32 错误码 {ctypes.get_last_error()}")
    try:
        return _blob_to(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def encrypt_dpapi(data: bytes, description: str = "DrCOM password") -> bytes:
    """Encrypt with the current user's DPAPI master key."""
    return _dpapi_call("CryptProtectData", data, description)


def decrypt_dpapi(data: bytes) -> bytes:
    """Decrypt a blob produced by :func:`encrypt_dpapi` for this user."""
    return _dpapi_call("CryptUnprotectData", data)


# --------------------------------------------------------------------------
# Cross-platform fallbacks
# --------------------------------------------------------------------------
def _machine_key(key_path: Path) -> bytes:
    """Load or create a local key file (non-Windows backends only)."""
    if key_path.exists():
        raw = key_path.read_bytes()
        if len(raw) >= 32:
            return raw[:32]
    key = os.urandom(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    try:
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - best effort on odd filesystems
        pass
    return key


def _fernet(key: bytes):
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key).digest()))


def _obfuscate(data: bytes, key: bytes) -> bytes:
    """Keyed keystream XOR — obfuscation, explicitly *not* encryption.

    Used only when no real crypto backend exists, and always reported as
    degraded so the UI can say so out loud.
    """
    out = bytearray(len(data))
    counter = 0
    while len(out) < len(data) or counter == 0:
        block = hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        for i, byte in enumerate(block):
            idx = counter * 32 + i
            if idx >= len(data):
                return bytes(out)
            out[idx] = data[idx] ^ byte
        counter += 1
        if counter * 32 >= len(data):
            break
    return bytes(out)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def protection_backend_name() -> str:
    if IS_WINDOWS:
        return "Windows DPAPI"
    if _fernet(b"\x00" * 32) is not None:
        return "Fernet (cryptography)"
    return "可逆混淆（降级）"


def protect(plaintext: str, *, key_path: Path) -> ProtectionResult:
    """Protect a secret for storage. Returns the base64 body plus metadata."""
    data = plaintext.encode("utf-8")
    if IS_WINDOWS:
        try:
            blob = _TAG_DPAPI + encrypt_dpapi(data)
            return ProtectionResult(base64.b64encode(blob).decode("ascii"), "Windows DPAPI")
        except (ProtectError, OSError):
            pass

    key = _machine_key(key_path)
    fernet = _fernet(key)
    if fernet is not None:
        blob = _TAG_FERNET + fernet.encrypt(data)
        return ProtectionResult(base64.b64encode(blob).decode("ascii"), "Fernet (cryptography)")

    blob = _TAG_OBFUSCATED + _obfuscate(data, key)
    return ProtectionResult(base64.b64encode(blob).decode("ascii"), "可逆混淆（降级）", degraded=True)


def unprotect(stored: str, *, key_path: Path) -> ProtectionResult:
    """Recover a secret previously produced by :func:`protect`."""
    if not stored:
        return ProtectionResult("", protection_backend_name())

    try:
        blob = base64.b64decode(stored, validate=True)
    except Exception as exc:
        raise ProtectError("密码字段不是合法的 Base64，配置可能已损坏") from exc

    if blob.startswith(_TAG_DPAPI):
        if not IS_WINDOWS:
            raise ProtectError("该密码由 Windows DPAPI 加密，只能在原来那台 Windows 机器/账号下解密")
        return ProtectionResult(decrypt_dpapi(blob[3:]).decode("utf-8"), "Windows DPAPI")

    key = _machine_key(key_path)

    if blob.startswith(_TAG_FERNET):
        fernet = _fernet(key)
        if fernet is None:
            raise ProtectError("该密码由 Fernet 加密，但当前环境缺少 cryptography 依赖")
        return ProtectionResult(fernet.decrypt(blob[3:]).decode("utf-8"), "Fernet (cryptography)")

    if blob.startswith(_TAG_OBFUSCATED):
        return ProtectionResult(
            _obfuscate(blob[3:], key).decode("utf-8"), "可逆混淆（降级）", degraded=True
        )

    # Legacy/untagged: the reference client stores a bare DPAPI blob.
    if IS_WINDOWS:
        try:
            return ProtectionResult(decrypt_dpapi(blob).decode("utf-8"), "Windows DPAPI")
        except Exception as exc:
            raise ProtectError("无法解密已保存的密码，请重新输入") from exc
    raise ProtectError("无法识别的密码存储格式，请重新输入密码")
