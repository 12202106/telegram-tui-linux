"""Command-line entry point for ``telegram-tui`` and ``python -m telegram_tui``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__
from .config import ConfigurationError, Settings, load_settings


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="用键盘操作的 Telegram 终端客户端")
    parser.add_argument("--demo", action="store_true", help="启动离线演示，不连接 Telegram")
    parser.add_argument("--session", metavar="PATH", help="会话文件路径（覆盖 TELEGRAM_SESSION）")
    parser.add_argument("--proxy", metavar="URL", help="SOCKS5 / HTTP 代理（覆盖 TELEGRAM_PROXY）")
    parser.add_argument("--version", action="version", version=f"telegram-tui {__version__}")
    args = parser.parse_args(argv)

    if args.demo:
        # A demo must not depend on, or accidentally use, real account settings.
        from .app import TelegramApp
        from .demo import DemoBackend

        TelegramApp(backend=DemoBackend(), settings=Settings()).run()
        return

    try:
        settings = load_settings(session_path=args.session, proxy=args.proxy)
    except ConfigurationError as error:
        parser.error(str(error))

    from .app import TelegramApp
    from .backend import TelegramBackend, describe_error

    backend = None
    if settings.has_credentials:
        try:
            backend = TelegramBackend(
                settings.api_id,
                settings.api_hash,
                settings.session_path,
                proxy=settings.proxy,
            )
        except Exception as error:
            parser.error(describe_error(error))
    TelegramApp(backend=backend, settings=settings).run()


if __name__ == "__main__":
    main()
