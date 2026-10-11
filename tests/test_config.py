"""Configuration validation and command-line wiring without network access."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from telegram_tui.__main__ import main
from telegram_tui.config import ConfigurationError, Settings, load_settings, parse_proxy

VALID_HASH = "0123456789abcdef0123456789abcdef"


def test_default_session_respects_xdg_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert Settings().session_path == tmp_path / ".local/share/telegram-tui/account.session"
    assert load_settings({}).session_path == Settings().session_path
    custom = tmp_path / "data"
    expected = custom / "telegram-tui/account.session"
    assert load_settings({"XDG_DATA_HOME": str(custom)}).session_path == expected
    monkeypatch.setenv("XDG_DATA_HOME", str(custom))
    assert Settings().session_path == expected
    assert load_settings({"XDG_DATA_HOME": "relative"}).session_path == (
        tmp_path / ".local/share/telegram-tui/account.session"
    )


def test_missing_and_partial_credentials_can_open_setup():
    assert not load_settings({}).has_credentials
    only_id = load_settings({"TELEGRAM_API_ID": "123456"})
    assert only_id.api_id == 123456
    assert not only_id.has_credentials
    only_hash = load_settings({"TELEGRAM_API_HASH": VALID_HASH})
    assert only_hash.api_hash == VALID_HASH
    assert not only_hash.has_credentials


def test_complete_credentials_are_parsed_and_secrets_hidden():
    settings = load_settings(
        {"TELEGRAM_API_ID": " 123456 ", "TELEGRAM_API_HASH": f" {VALID_HASH} "}
    )
    assert settings.has_credentials
    assert settings.api_id == 123456
    assert settings.api_hash == VALID_HASH
    assert VALID_HASH not in repr(settings)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "secret-text", "１２３", "1e3"])
def test_invalid_api_id_is_rejected_without_echo(value):
    with pytest.raises(ConfigurationError, match="TELEGRAM_API_ID") as caught:
        load_settings({"TELEGRAM_API_ID": value})
    assert value not in str(caught.value)


@pytest.mark.parametrize("value", ["private-secret", "z" * 32, "a" * 31, "a" * 33])
def test_invalid_api_hash_is_rejected_without_echo(value):
    with pytest.raises(ConfigurationError, match="TELEGRAM_API_HASH") as caught:
        load_settings({"TELEGRAM_API_HASH": value})
    assert value not in str(caught.value)


def test_explicit_options_override_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = load_settings(
        {"TELEGRAM_SESSION": "/other/account.session", "TELEGRAM_PROXY": "invalid"},
        session_path="~/private/override.session",
        proxy="socks5://127.0.0.1:1080",
    )
    assert settings.session_path == tmp_path / "private/override.session"
    assert settings.proxy == {
        "proxy_type": "socks5",
        "addr": "127.0.0.1",
        "port": 1080,
        "rdns": True,
    }
    assert load_settings({"TELEGRAM_PROXY": "invalid"}, proxy="").proxy is None


def test_proxy_supports_authenticated_http_and_ipv6():
    proxy = parse_proxy("http://user%40mail:private%3Asecret@[::1]:8080")
    assert proxy == {
        "proxy_type": "http",
        "addr": "::1",
        "port": 8080,
        "rdns": True,
        "username": "user@mail",
        "password": "private:secret",
    }
    assert "private:secret" not in repr(Settings(proxy=proxy))


@pytest.mark.parametrize(
    "value",
    [
        "https://user:private-secret@localhost:8080",
        "socks5://user:private-secret@localhost",
        "socks5://user:private-secret@localhost:65536",
        "socks5://user:private-secret@localhost:0",
        "socks5://user:private-secret@localhost:private-secret",
        "socks5://user:private-secret@localhost:1080/path",
        "socks5://user:private-secret@localhost:1080?secret=yes",
        "socks5://user:private-secret@localhost:1080#fragment",
        "socks5://user:private-secret@localhost name:1080",
        "socks5://user:private-secret@[invalid:1080",
    ],
)
def test_invalid_proxy_errors_do_not_reveal_credentials(value):
    with pytest.raises(ConfigurationError) as caught:
        parse_proxy(value)
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_empty_proxy_disables_proxy(value):
    assert parse_proxy(value) is None


@pytest.fixture
def fake_ui(monkeypatch):
    instances = []
    backends = []

    class FakeApp:
        def __init__(self, *, backend, settings):
            self.backend = backend
            self.settings = settings
            self.ran = False
            instances.append(self)

        def run(self):
            self.ran = True

    class FakeBackend:
        def __init__(self, *args, **kwargs):
            backends.append((args, kwargs))

    class FakeDemo:
        pass

    for name, attribute, value in (
        ("telegram_tui.app", "TelegramApp", FakeApp),
        ("telegram_tui.backend", "TelegramBackend", FakeBackend),
        ("telegram_tui.demo", "DemoBackend", FakeDemo),
    ):
        module = ModuleType(name)
        setattr(module, attribute, value)
        if name == "telegram_tui.backend":
            from telegram_tui.backend import describe_error

            module.describe_error = describe_error
        monkeypatch.setitem(sys.modules, name, module)
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION", "TELEGRAM_PROXY"):
        monkeypatch.delenv(key, raising=False)
    return instances, backends, FakeDemo


def test_cli_without_credentials_opens_setup(fake_ui):
    instances, backends, _ = fake_ui
    main([])
    assert instances[0].ran
    assert instances[0].backend is None
    assert not instances[0].settings.has_credentials
    assert backends == []


def test_cli_builds_backend_with_explicit_overrides(fake_ui, monkeypatch, tmp_path):
    instances, backends, _ = fake_ui
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", VALID_HASH)
    session = tmp_path / "custom.session"
    main(["--session", str(session), "--proxy", "http://localhost:8080"])
    assert instances[0].ran
    assert backends[0][0] == (123456, VALID_HASH, Path(session))
    assert backends[0][1]["proxy"]["port"] == 8080


def test_session_storage_error_exits_cleanly_without_sensitive_details(
    fake_ui, monkeypatch, capsys
):
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", VALID_HASH)

    def inaccessible_backend(*args, **kwargs):
        raise PermissionError("private-account-path")

    monkeypatch.setattr(
        sys.modules["telegram_tui.backend"], "TelegramBackend", inaccessible_backend
    )
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2
    stderr = capsys.readouterr().err
    assert "会话目录的权限" in stderr
    assert "private-account-path" not in stderr


def test_demo_ignores_real_account_environment(fake_ui, monkeypatch):
    instances, backends, fake_demo = fake_ui
    monkeypatch.setenv("TELEGRAM_API_ID", "invalid-private-value")
    monkeypatch.setenv("TELEGRAM_PROXY", "private-invalid-proxy")
    main(["--demo"])
    assert instances[0].ran
    assert isinstance(instances[0].backend, fake_demo)
    assert not instances[0].settings.has_credentials
    assert backends == []


def test_invalid_configuration_exits_cleanly(fake_ui, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_API_HASH", "private-secret")
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2
    stderr = capsys.readouterr().err
    assert "TELEGRAM_API_HASH" in stderr
    assert "private-secret" not in stderr
    assert "Traceback" not in stderr
    assert fake_ui[0] == []


def test_version_does_not_need_configuration(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_API_ID", "invalid-private-value")
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert "telegram-tui 0.1.0" in capsys.readouterr().out
