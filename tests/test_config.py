"""Config persistence and password-at-rest protection.

Acceptance criterion 6 lives here: the password must not be readable in the
config file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drcom.config import Account, AppConfig, ConfigStore, default_data_dir
from drcom.secrets_store import ProtectError, protect, unprotect


# --------------------------------------------------------------------------
# secret storage
# --------------------------------------------------------------------------
def test_password_round_trip(tmp_path: Path) -> None:
    key = tmp_path / "key.bin"
    result = protect("s3cret-密码", key_path=key)
    assert result.value
    assert "s3cret" not in result.value
    recovered = unprotect(result.value, key_path=key)
    assert recovered.value == "s3cret-密码"


def test_password_is_not_plaintext_in_the_blob(tmp_path: Path) -> None:
    result = protect("SuperSecret123", key_path=tmp_path / "key.bin")
    import base64

    raw = base64.b64decode(result.value)
    assert b"SuperSecret123" not in raw


def test_windows_uses_dpapi(tmp_path: Path) -> None:
    import sys

    result = protect("whatever", key_path=tmp_path / "key.bin")
    if sys.platform == "win32":
        assert result.backend == "Windows DPAPI"
        assert not result.degraded
    else:
        pytest.skip("DPAPI is Windows-only")


def test_dpapi_blob_is_not_portable_to_another_machine(tmp_path: Path) -> None:
    """A copied config must be useless elsewhere — that is the whole point."""
    result = protect("whatever", key_path=tmp_path / "key.bin")
    import base64

    import sys

    if sys.platform != "win32":
        pytest.skip("DPAPI is Windows-only")
    blob = base64.b64decode(result.value)
    assert blob.startswith(b"D1:")
    # Tamper with the ciphertext: decryption must fail, not return garbage.
    broken = bytearray(blob)
    broken[-5] ^= 0xFF
    with pytest.raises(ProtectError):
        unprotect(base64.b64encode(bytes(broken)).decode(), key_path=tmp_path / "key.bin")


def test_unprotect_rejects_garbage(tmp_path: Path) -> None:
    with pytest.raises(ProtectError):
        unprotect("not-base64!!!", key_path=tmp_path / "key.bin")
    with pytest.raises(ProtectError):
        unprotect("QUJD", key_path=tmp_path / "key.bin")  # valid base64, wrong content


def test_empty_password_is_handled(tmp_path: Path) -> None:
    assert unprotect("", key_path=tmp_path / "key.bin").value == ""


# --------------------------------------------------------------------------
# config store
# --------------------------------------------------------------------------
def test_config_defaults_are_the_campus_settings() -> None:
    config = AppConfig()
    assert config.auth.server == "10.100.61.3"
    assert config.auth.port == 61440
    assert config.auth.bind_port == 61440
    assert config.auth.timeout_ms == 3000
    assert config.auth.keepalive_interval == 20.0


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.load()
    store.config.active_account().account = "2023000001"
    store.config.auth.keepalive_interval = 25.0
    store.config.ui.reduce_motion = True
    store.save()

    reopened = ConfigStore(tmp_path).load()
    assert reopened.active_account().account == "2023000001"
    assert reopened.auth.keepalive_interval == 25.0
    assert reopened.ui.reduce_motion is True


def test_config_file_contains_no_plaintext_password(tmp_path: Path) -> None:
    """Acceptance criterion 6."""
    store = ConfigStore(tmp_path)
    store.load()
    account = store.config.active_account()
    account.account = "2023000001"
    account.mac = "AA:BB:CC:DD:EE:FF"
    store.set_password(account.id, "MyPlainTextPassword")
    store.save()

    text = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "MyPlainTextPassword" not in text
    assert "password_protected" in text

    # And it still decrypts for the current user.
    reopened = ConfigStore(tmp_path)
    reopened.load()
    assert reopened.get_password(reopened.config.active_account().id) == "MyPlainTextPassword"


def test_corrupt_config_is_backed_up_not_deleted(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{ this is not json", encoding="utf-8")
    store = ConfigStore(tmp_path)
    config = store.load()
    # A fresh config is handed back, but with one blank account so the UI has
    # something to bind to.
    assert len(config.accounts) == 1
    assert config.accounts[0].account == ""
    assert store.load_error
    assert (tmp_path / "config.json.broken").exists()


def test_unknown_keys_are_tolerated(tmp_path: Path) -> None:
    payload = {
        "version": 99,
        "brand_new_section": {"future": True},
        "auth": {"server": "10.0.0.1", "unknown_field": 5},
        "accounts": [],
    }
    (tmp_path / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    config = ConfigStore(tmp_path).load()
    assert config.auth.server == "10.0.0.1"


def test_account_masking() -> None:
    assert Account(account="").masked_account == "(未设置)"
    assert Account(account="ab").masked_account == "**"
    assert Account(account="12345").masked_account == "1****"
    assert Account(account="2023000001").masked_account == "20******01"
    # The real account must not survive masking.
    assert "230000" not in Account(account="2023000001").masked_account


def test_account_management(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.load()
    first = store.config.active_account()
    second = store.add_account(Account(account="2024000002"), make_active=True)
    assert store.config.active_account() is second
    assert len(store.config.accounts) == 2

    assert store.remove_account(second.id)
    assert store.config.active_account().id == first.id

    # Removing the last account leaves an empty list; ensure_account refills it.
    store.remove_account(first.id)
    assert store.config.accounts == []
    assert store.config.ensure_account() is not None


def test_set_password_updates_the_cache(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.load()
    account = store.config.active_account()
    store.set_password(account.id, "first")
    assert store.get_password(account.id) == "first"
    store.set_password(account.id, "second")
    assert store.get_password(account.id) == "second"


def test_default_data_dir_is_absolute() -> None:
    assert default_data_dir().is_absolute()


def test_save_is_atomic(tmp_path: Path) -> None:
    """A leftover temp file must never be mistaken for the real config."""
    store = ConfigStore(tmp_path)
    store.load()
    store.save()
    assert not (tmp_path / "config.json.tmp").exists()
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["version"] == 1


# --------------------------------------------------------------------------
# the project was renamed to jlu-drcom-ng; existing installs must not break
# --------------------------------------------------------------------------
def test_legacy_data_dir_is_migrated(tmp_path: Path) -> None:
    """A pre-rename install keeps its config instead of silently starting fresh."""
    from drcom.config import migrate_legacy_data_dir

    base = tmp_path / "Roaming"
    legacy = base / "DrCOM-JLU"
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text('{"version": 1}', encoding="utf-8")
    (legacy / "key.bin").write_bytes(b"secret-key-material")

    primary = base / "JLU-DrCOM-NG"
    note = migrate_legacy_data_dir(primary)

    assert note, "migration should report what it did"
    assert primary.exists()
    assert (primary / "config.json").read_text(encoding="utf-8") == '{"version": 1}'
    assert (primary / "key.bin").read_bytes() == b"secret-key-material"
    assert not legacy.exists()


def test_migration_is_a_no_op_when_the_new_dir_has_data(tmp_path: Path) -> None:
    from drcom.config import migrate_legacy_data_dir

    base = tmp_path / "Roaming"
    primary = base / "JLU-DrCOM-NG"
    primary.mkdir(parents=True)
    (primary / "config.json").write_text('{"version": 1, "mine": true}', encoding="utf-8")

    legacy = base / "DrCOM-JLU"
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text('{"version": 1, "old": true}', encoding="utf-8")

    assert migrate_legacy_data_dir(primary) == ""
    assert "mine" in (primary / "config.json").read_text(encoding="utf-8")
    assert legacy.exists(), "the old directory should be left alone"


def test_migration_is_a_no_op_on_a_clean_machine(tmp_path: Path) -> None:
    from drcom.config import migrate_legacy_data_dir

    assert migrate_legacy_data_dir(tmp_path / "nothing-here") == ""


def test_app_name_is_the_new_one() -> None:
    from drcom.config import APP_NAME, APP_SLUG, LEGACY_APP_NAME

    assert APP_NAME == "JLU-DrCOM-NG"
    assert APP_SLUG == "jlu-drcom-ng"
    assert LEGACY_APP_NAME == "DrCOM-JLU"
