"""Environment configuration without persisting API credentials."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit


class ConfigurationError(ValueError):
    """An actionable configuration error safe to show in the terminal."""


def _default_session(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    data_home = env.get("XDG_DATA_HOME", "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    # XDG specifies an absolute path; ignore relative values instead of writing
    # an account session under an unexpected working directory.
    if not base.is_absolute():
        base = Path.home() / ".local" / "share"
    return base / "telegram-tui" / "account.session"


@dataclass(slots=True)
class Settings:
    api_id: int | None = None
    api_hash: str = field(default="", repr=False)
    session_path: Path = field(default_factory=_default_session)
    proxy: dict[str, object] | None = field(default=None, repr=False)

    @property
    def has_credentials(self) -> bool:
        return self.api_id is not None and bool(self.api_hash)


def parse_proxy(value: str | None) -> dict[str, object] | None:
    """Convert a proxy URL to Telethon's proxy options without leaking secrets."""
    if not value or not value.strip():
        return None
    value = value.strip()
    message = "代理格式无效，请使用 socks5://host:port 或 http://host:port。"
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"socks5", "http"}
            or not parts.hostname
            or parts.port is None
            or not 1 <= parts.port <= 65535
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or any(character.isspace() for character in value)
        ):
            raise ConfigurationError(message)
        proxy: dict[str, object] = {
            "proxy_type": parts.scheme,
            "addr": parts.hostname,
            "port": parts.port,
            "rdns": True,
        }
        if parts.username is not None:
            proxy["username"] = unquote(parts.username)
        if parts.password is not None:
            proxy["password"] = unquote(parts.password)
        return proxy
    except ValueError:
        # urllib errors can include the supplied port or URL. Replace them with
        # a fixed message because URLs may contain proxy credentials.
        raise ConfigurationError(message) from None


def load_settings(
    environ: Mapping[str, str] | None = None,
    *,
    session_path: str | Path | None = None,
    proxy: str | None = None,
) -> Settings:
    """Read environment values; explicit command-line options take precedence.

    A valid but incomplete API pair is allowed: the setup screen requests the
    missing field. Malformed supplied values are reported before connecting.
    """
    env = os.environ if environ is None else environ
    raw_id = env.get("TELEGRAM_API_ID", "").strip()
    api_id = None
    if raw_id:
        if not raw_id.isascii() or not raw_id.isdigit():
            raise ConfigurationError("TELEGRAM_API_ID 必须是正整数。")
        try:
            api_id = int(raw_id)
        except ValueError:
            raise ConfigurationError("TELEGRAM_API_ID 必须是正整数。") from None
        if api_id <= 0:
            raise ConfigurationError("TELEGRAM_API_ID 必须是正整数。")
    api_hash = env.get("TELEGRAM_API_HASH", "").strip()
    if api_hash and re.fullmatch(r"[0-9a-fA-F]{32}", api_hash) is None:
        raise ConfigurationError("TELEGRAM_API_HASH 必须是 32 位十六进制字符串。")

    selected_session = (
        session_path if session_path is not None else env.get("TELEGRAM_SESSION", "").strip()
    )
    selected_proxy = proxy if proxy is not None else env.get("TELEGRAM_PROXY", "")
    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        session_path=(
            Path(selected_session).expanduser() if selected_session else _default_session(env)
        ),
        proxy=parse_proxy(selected_proxy),
    )
