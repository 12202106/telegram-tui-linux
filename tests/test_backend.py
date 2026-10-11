"""Backend checks use a fake Telethon client; no account or network is needed."""

import asyncio
import os
import stat
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors

from telegram_tui import backend as module
from telegram_tui.backend import CodeNotRequestedError, TelegramBackend, describe_error


async def iterate(items):
    for item in items:
        yield item


def message(message_id=1, text="hello", **kwargs):
    fields = dict(
        id=message_id,
        raw_text=text,
        message=text,
        date=datetime(2026, 1, 2, tzinfo=UTC),
        out=False,
        media=None,
        sender=NS(first_name="Ada", last_name="Lovelace"),
        sender_id=20,
    )
    fields.update(kwargs)
    return NS(**fields)


@pytest.fixture
def client(monkeypatch):
    fake = NS(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        add_event_handler=Mock(),
        remove_event_handler=Mock(),
        session=NS(close=Mock()),
        send_code_request=AsyncMock(return_value=NS(phone_code_hash="code-hash")),
        sign_in=AsyncMock(),
        get_me=AsyncMock(),
        send_message=AsyncMock(),
        send_read_acknowledge=AsyncMock(),
        iter_messages=Mock(),
        iter_dialogs=Mock(),
    )
    factory = Mock(return_value=fake)
    monkeypatch.setattr(module, "TelegramClient", factory)
    fake.factory = factory
    return fake


@pytest.fixture
def backend(tmp_path, client):
    return TelegramBackend(12345, "private-api-hash", tmp_path / "telegram" / "account")


async def test_connect_reports_authorization_and_registers_once(backend, client):
    assert await backend.connect() is False
    client.is_user_authorized.return_value = True
    assert await backend.connect() is True
    client.add_event_handler.assert_called_once()
    assert client.add_event_handler.call_args.args[1].incoming is None
    assert client.add_event_handler.call_args.args[1].outgoing is None


@pytest.mark.parametrize("phase", ["connect", "is_user_authorized"])
async def test_startup_timeout_cancels_transport_and_allows_retry(
    backend, client, monkeypatch, phase
):
    monkeypatch.setattr(module, "_CONNECT_TIMEOUT", 0.01)
    cancelled = asyncio.Event()

    async def stalled():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    getattr(client, phase).side_effect = stalled
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(backend.connect(), timeout=1)
    assert cancelled.is_set()
    client.disconnect.assert_awaited_once()
    # Unlike final shutdown, the adapter does not close the session itself.
    client.session.close.assert_not_called()
    assert stat.S_IMODE(backend.session_path.stat().st_mode) == 0o600

    getattr(client, phase).side_effect = None
    client.is_user_authorized.return_value = False
    assert await backend.connect() is False
    client.add_event_handler.assert_called_once()
    assert client.connect.await_count == 2


async def test_startup_cleanup_is_bounded_when_disconnect_stalls(
    backend, client, monkeypatch, caplog
):
    monkeypatch.setattr(module, "_CONNECT_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "_DISCONNECT_TIMEOUT", 0.01)
    cleanup_cancelled = asyncio.Event()

    async def stalled_connect():
        await asyncio.Future()

    async def stalled_disconnect():
        try:
            await asyncio.Future()
        finally:
            cleanup_cancelled.set()

    client.connect.side_effect = stalled_connect
    client.disconnect.side_effect = stalled_disconnect
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(backend.connect(), timeout=1)
    assert cleanup_cancelled.is_set()
    assert "Startup connection cleanup failed: TimeoutError" in caplog.text
    client.session.close.assert_not_called()


async def test_startup_cleanup_failure_does_not_hide_timeout(backend, client, monkeypatch, caplog):
    monkeypatch.setattr(module, "_CONNECT_TIMEOUT", 0.01)

    async def stalled():
        await asyncio.Future()

    client.connect.side_effect = stalled
    client.disconnect.side_effect = ConnectionError("secret-api-hash")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(backend.connect(), timeout=1)
    assert "ConnectionError" in caplog.text
    assert "secret-api-hash" not in caplog.text


async def test_cancelled_startup_cleans_partial_connection(backend, client):
    started = asyncio.Event()

    async def stalled():
        started.set()
        await asyncio.Future()

    client.connect.side_effect = stalled
    connection = asyncio.create_task(backend.connect())
    await started.wait()
    connection.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connection
    client.disconnect.assert_awaited_once()
    client.session.close.assert_not_called()


async def test_code_login_preserves_request_hash_and_clears_it_on_success(backend, client):
    await backend.request_code("+8612345678901")
    assert await backend.sign_in("12345") is True
    client.sign_in.assert_awaited_once_with(
        phone="+8612345678901",
        code="12345",
        phone_code_hash="code-hash",
    )
    with pytest.raises(CodeNotRequestedError):
        await backend.sign_in("12345")


async def test_two_factor_transition_and_password(backend, client):
    await backend.request_code("+12025550123")
    client.sign_in.side_effect = errors.SessionPasswordNeededError(request=None)
    assert await backend.sign_in("12345") is False
    client.sign_in.side_effect = None
    await backend.sign_in_password("password-never-logged")
    client.sign_in.assert_awaited_with(password="password-never-logged")
    assert backend._phone is None
    assert backend._phone_code_hash is None


async def test_login_errors_are_propagated_for_ui(backend, client):
    await backend.request_code("+12025550123")
    client.sign_in.side_effect = errors.PhoneCodeInvalidError(request=None)
    with pytest.raises(errors.PhoneCodeInvalidError):
        await backend.sign_in("wrong")
    assert backend._phone_code_hash == "code-hash"


async def test_self_user_name_and_optional_username(backend, client):
    client.get_me.return_value = NS(id=12, first_name="Lin", last_name="Chen", username=None)
    user = await backend.get_me()
    assert (user.id, user.name, user.username) == (12, "Lin Chen", "")
    client.get_me.return_value = None
    with pytest.raises(module.LoginRequiredError):
        await backend.get_me()


async def test_history_paging_chronological_order_and_literal_text(backend, client):
    literal = "  **literal** [red]markup[/red]\n<script>  "
    client.iter_messages.return_value = iterate([message(9, literal), message(8, "older")])
    result = await backend.get_messages(-1001234, limit=2, before_id=10)
    client.iter_messages.assert_called_once_with(-1001234, limit=2, offset_id=10)
    assert [item.id for item in result] == [8, 9]
    assert result[1].text == literal
    assert result[1].sender == "Ada Lovelace"


async def test_send_uses_literal_content_without_link_preview(backend, client):
    literal = "**bold?** [markup] `code`\n <hello>"
    client.send_message.return_value = message(42, literal, out=True)
    backend._chat_titles[88] = "Saved messages"
    result = await backend.send_message(88, literal)
    client.send_message.assert_awaited_once_with(
        88,
        literal,
        parse_mode=None,
        link_preview=False,
    )
    assert result.text == literal
    assert result.outgoing is True
    assert result.sender == "我"
    assert result.chat_title == "Saved messages"
    await backend.mark_read(88)
    client.send_read_acknowledge.assert_awaited_once_with(88)


async def test_caption_and_attachment_description(backend, client):
    client.iter_messages.return_value = iterate(
        [
            message(2, "  photo caption  ", media=object(), photo=object()),
            message(1, "", media=object(), file=NS(name="[literal].pdf")),
        ]
    )
    result = await backend.get_messages(10)
    assert result[0].text == ""
    assert result[0].media == "文件：[literal].pdf"
    assert result[1].text == "  photo caption  "
    assert result[1].media == "照片"


def dialog(chat_id, entity, *, is_group=False, last=None):
    return NS(
        id=chat_id,
        name=f"chat {chat_id}",
        entity=entity,
        is_group=is_group,
        message=last,
        unread_count=3,
        date=None,
    )


async def test_dialog_channel_permissions_and_media_preview(backend, client):
    client.iter_dialogs.return_value = iterate(
        [
            dialog(1, NS(), last=message(text="  [literal]  ")),
            dialog(2, NS(broadcast=True)),
            dialog(3, NS(broadcast=True, creator=True)),
            dialog(4, NS(broadcast=True, admin_rights=NS(post_messages=True))),
            dialog(5, NS(broadcast=True, admin_rights=NS(post_messages=False))),
            dialog(6, NS(default_banned_rights=NS(send_plain=True)), is_group=True),
            dialog(
                7, NS(admin_rights=NS(), default_banned_rights=NS(send_plain=True)), is_group=True
            ),
            dialog(8, NS(), last=message(text="", media=object(), photo=object())),
        ]
    )
    result = await backend.get_dialogs(limit=8)
    client.iter_dialogs.assert_called_once_with(limit=8)
    assert [item.can_send for item in result] == [True, False, True, True, False, False, True, True]
    assert result[0].preview == "  [literal]  "
    assert result[1].kind == "channel"
    assert result[5].kind == "group"
    assert result[7].preview == "照片"
    assert backend._chat_titles[8] == "chat 8"


async def test_events_resolve_new_chat_and_deliver_outgoing_messages(backend):
    handler = AsyncMock()
    backend.set_message_handler(handler)
    event = NS(
        chat_id=45, message=message(out=True), get_chat=AsyncMock(return_value=NS(title="新群"))
    )
    await backend._on_new_message(event)
    received = handler.call_args.args[0]
    assert (received.chat_title, received.sender, received.outgoing) == ("新群", "我", True)
    assert backend._chat_titles[45] == "新群"


async def test_event_handler_failure_does_not_escape_or_log_content(backend, caplog):
    secret = "sensitive message and password"
    backend.set_message_handler(AsyncMock(side_effect=RuntimeError(secret)))
    event = NS(
        chat_id=45, message=message(text=secret), get_chat=AsyncMock(side_effect=ValueError())
    )
    await backend._on_new_message(event)
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


async def test_missing_sender_keeps_message_when_entity_lookup_fails(backend):
    raw = message(sender=None, get_sender=AsyncMock(side_effect=ValueError()), sender_id=123)
    converted = await backend._to_message(raw, 45)
    assert converted.sender == "123"
    assert converted.text == "hello"


async def test_disconnect_closes_session_even_if_network_cleanup_fails(backend, client):
    await backend.connect()
    backend._phone = "secret-phone"
    client.disconnect.side_effect = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        await backend.disconnect()
    client.remove_event_handler.assert_called_once()
    client.session.close.assert_called_once()
    assert backend._phone is None


def test_proxy_forwarded_and_session_path_normalized(tmp_path, client):
    proxy = {"proxy_type": "socks5", "addr": "localhost", "port": 1080}
    account = tmp_path / "account.session"
    TelegramBackend(123, "hash", account, proxy=proxy)
    client.factory.assert_called_once_with(
        str(account),
        123,
        "hash",
        proxy=proxy,
        flood_sleep_threshold=0,
        connection_retries=3,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_new_session_and_directory_are_private(backend):
    assert stat.S_IMODE(backend.session_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backend.session_path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_parent_is_not_chmodded_but_session_is_secured(tmp_path, client):
    tmp_path.chmod(0o755)
    session = tmp_path / "account.session"
    session.write_text("session-data")
    session.chmod(0o644)
    TelegramBackend(123, "hash", session)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    assert stat.S_IMODE(session.stat().st_mode) == 0o600
    assert session.read_text() == "session-data"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="Requires O_NOFOLLOW")
def test_session_symlink_does_not_change_target_permissions(tmp_path, client):
    target = tmp_path / "unrelated"
    target.write_text("do not touch")
    target.chmod(0o644)
    (tmp_path / "account.session").symlink_to(target)
    with pytest.raises(OSError):
        TelegramBackend(123, "hash", tmp_path / "account")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (errors.FloodWaitError(request=None, capture=17), "17 秒"),
        (errors.PhoneCodeInvalidError(request=None), "验证码不正确"),
        (errors.PhoneCodeExpiredError(request=None), "验证码已过期"),
        (errors.PhoneNumberInvalidError(request=None), "国家区号"),
        (errors.PasswordHashInvalidError(request=None), "密码不正确"),
        (TimeoutError("api_hash=secret"), "连接 Telegram 超时"),
        (ConnectionError("api_hash=secret"), "连接失败"),
        (RuntimeError("api_hash=secret"), "操作失败"),
    ],
)
def test_errors_are_friendly_and_never_echo_secrets(exception, expected):
    result = describe_error(exception)
    assert expected in result
    assert "secret" not in result
