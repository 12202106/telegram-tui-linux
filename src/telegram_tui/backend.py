"""Telegram transport, with no dependency on Textual or terminal rendering."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors, events

from .models import ChatMessage, Dialog, MessageHandler, User

_log = logging.getLogger(__name__)
_CONNECT_TIMEOUT = 20.0
_DISCONNECT_TIMEOUT = 5.0


class CodeNotRequestedError(RuntimeError):
    """A verification code has not yet been requested in this login flow."""


class LoginRequiredError(RuntimeError):
    """The stored session is not authorized."""


def describe_error(exc: Exception) -> str:
    """Return a useful message without echoing secrets from exception details."""
    if isinstance(exc, (errors.FloodWaitError, errors.SlowModeWaitError)):
        return f"Telegram 请求过于频繁，请等待 {exc.seconds} 秒后重试。"
    messages = {
        "PhoneNumberInvalidError": "手机号格式不正确，请使用包含国家区号的完整号码。",
        "PhoneNumberBannedError": "此手机号已被 Telegram 封禁。",
        "PhoneCodeInvalidError": "验证码不正确，请重新输入。",
        "PhoneCodeEmptyError": "请输入收到的验证码。",
        "PhoneCodeExpiredError": "验证码已过期，请重新发送验证码。",
        "PasswordHashInvalidError": "两步验证密码不正确，请重新输入。",
        "SessionPasswordNeededError": "此账号启用了两步验证，请输入密码。",
        "ApiIdInvalidError": "API ID 或 API Hash 不正确，请检查本地配置。",
        "ApiIdPublishedFloodError": "此 API ID 已被 Telegram 限制，请使用自己的 API 凭据。",
        "PhoneNumberFloodError": "验证码请求过于频繁，请稍后再试。",
        "PhonePasswordFloodError": "密码尝试过于频繁，请稍后再试。",
        "AuthKeyUnregisteredError": "登录会话已失效，请重新登录。",
        "SessionRevokedError": "登录会话已被撤销，请重新登录。",
        "AuthKeyDuplicatedError": "此登录会话已失效，请重新登录并避免同时复制使用会话文件。",
        "UserDeactivatedError": "此 Telegram 账号已停用。",
        "UserDeactivatedBanError": "此 Telegram 账号已被封禁。",
        "ChatWriteForbiddenError": "你目前没有向此会话发送消息的权限。",
        "ChatAdminRequiredError": "此操作需要管理员权限。",
        "UserBannedInChannelError": "你目前无法在此群组或频道发言。",
        "MessageTooLongError": "消息太长，请拆分后发送。",
        "MessageEmptyError": "不能发送空消息。",
        "PeerIdInvalidError": "暂时无法访问此会话，请刷新会话列表后重试。",
        "ChannelPrivateError": "此群组或频道不可访问，可能已退出或被移除。",
        "CodeNotRequestedError": "请先发送登录验证码。",
        "LoginRequiredError": "请先登录 Telegram。",
    }
    if type(exc).__name__ in messages:
        return messages[type(exc).__name__]
    if isinstance(exc, PermissionError):
        return "无法读写会话文件，请检查会话目录的权限。"
    if isinstance(exc, TimeoutError):
        return "连接 Telegram 超时，请检查网络或代理设置后重试。"
    if isinstance(exc, (ConnectionError, OSError)):
        return "连接失败，请检查网络或代理设置后重试。"
    if isinstance(exc, errors.RPCError):
        return "Telegram 暂时无法完成此请求，请稍后重试。"
    return "操作失败，请检查连接和配置后重试。"


def _display_name(entity: Any, fallback: str = "未知用户") -> str:
    if entity is None:
        return fallback
    if title := getattr(entity, "title", None):
        return str(title)
    name = " ".join(
        part
        for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None))
        if part
    )
    return name or getattr(entity, "username", None) or fallback


def _media_description(message: Any) -> str | None:
    if not getattr(message, "media", None):
        if getattr(message, "action", None):
            return "系统消息"
        return None
    for attribute, label in (
        ("photo", "照片"),
        ("voice", "语音"),
        ("video_note", "视频消息"),
        ("sticker", "贴纸"),
        ("gif", "动画"),
        ("video", "视频"),
        ("audio", "音频"),
        ("poll", "投票"),
        ("contact", "联系人"),
        ("geo", "位置"),
        ("venue", "地点"),
        ("web_preview", "链接预览"),
    ):
        if getattr(message, attribute, None):
            return label
    if file := getattr(message, "file", None):
        name = getattr(file, "name", None)
        return f"文件：{name}" if name else "文件"
    return "附件"


def _can_send(entity: Any, kind: str) -> bool:
    if any(getattr(entity, flag, False) for flag in ("left", "kicked", "deactivated")):
        return False
    if kind == "private":
        return not bool(getattr(entity, "deleted", False))
    creator = bool(getattr(entity, "creator", False))
    admin = getattr(entity, "admin_rights", None)
    if kind == "channel":
        return creator or bool(admin and getattr(admin, "post_messages", False))
    banned = getattr(entity, "banned_rights", None)
    defaults = getattr(entity, "default_banned_rights", None) if not (creator or admin) else None
    return not any(
        getattr(rights, flag, False)
        for rights in (banned, defaults)
        if rights
        for flag in ("send_messages", "send_plain")
    )


class TelegramBackend:
    """An explicit, non-interactive Telethon login and messaging adapter."""

    is_demo = False

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_path: Path | str,
        proxy: dict | None = None,
    ) -> None:
        path = Path(session_path).expanduser()
        self.session_path = path if str(path).endswith(".session") else Path(f"{path}.session")
        self._prepare_session_file()
        self._client = TelegramClient(
            str(self.session_path),
            api_id,
            api_hash,
            proxy=proxy,
            flood_sleep_threshold=0,
            connection_retries=3,
        )
        self._handler: MessageHandler | None = None
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self._chat_titles: dict[int, str] = {}
        self._events_registered = False

    def _prepare_session_file(self) -> None:
        parent = self.session_path.parent
        was_missing = not parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # An existing parent may be the user's home or a shared project directory.
        if was_missing:
            parent.chmod(0o700)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.session_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def _protect_session_files(self) -> None:
        # SQLite journal files carry the same sensitive account data as the session.
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(f"{self.session_path}{suffix}")
            if path.exists() and not path.is_symlink():
                path.chmod(0o600)

    async def connect(self) -> bool:
        if not self._events_registered:
            self._client.add_event_handler(self._on_new_message, events.NewMessage())
            self._events_registered = True
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT):
                await self._client.connect()
                return await self._client.is_user_authorized()
        except (TimeoutError, asyncio.CancelledError):
            # A timed-out handshake may leave transport tasks running. Telethon
            # can reopen its SQLite session when the same client retries.
            try:
                async with asyncio.timeout(_DISCONNECT_TIMEOUT):
                    await self._client.disconnect()
            except Exception as exc:
                _log.warning("Startup connection cleanup failed: %s", type(exc).__name__)
            raise
        finally:
            self._protect_session_files()

    async def disconnect(self) -> None:
        if self._events_registered:
            self._client.remove_event_handler(self._on_new_message)
            self._events_registered = False
        try:
            await self._client.disconnect()
        finally:
            self._phone = None
            self._phone_code_hash = None
            try:
                self._client.session.close()
            finally:
                self._protect_session_files()

    async def request_code(self, phone: str) -> None:
        sent = await self._client.send_code_request(phone)
        self._phone = phone
        self._phone_code_hash = sent.phone_code_hash
        self._protect_session_files()

    async def sign_in(self, code: str) -> bool:
        if not self._phone or not self._phone_code_hash:
            raise CodeNotRequestedError()
        try:
            await self._client.sign_in(
                phone=self._phone,
                code=code,
                phone_code_hash=self._phone_code_hash,
            )
        except errors.SessionPasswordNeededError:
            return False
        finally:
            self._protect_session_files()
        self._phone = None
        self._phone_code_hash = None
        return True

    async def sign_in_password(self, password: str) -> None:
        try:
            await self._client.sign_in(password=password)
        finally:
            self._protect_session_files()
        self._phone = None
        self._phone_code_hash = None

    async def get_me(self) -> User:
        me = await self._client.get_me()
        if me is None:
            raise LoginRequiredError()
        return User(me.id, _display_name(me), getattr(me, "username", None) or "")

    async def get_dialogs(self, limit: int = 200) -> list[Dialog]:
        dialogs = []
        async for dialog in self._client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if getattr(entity, "broadcast", False):
                kind = "channel"
            elif getattr(dialog, "is_group", False):
                kind = "group"
            else:
                kind = "private"
            name = dialog.name or _display_name(entity, str(dialog.id))
            self._chat_titles[dialog.id] = name
            last = dialog.message
            preview = getattr(last, "raw_text", None) or getattr(last, "message", "") or ""
            if not preview and last is not None:
                preview = _media_description(last) or ""
            dialogs.append(
                Dialog(
                    id=dialog.id,
                    name=name,
                    preview=preview,
                    unread=dialog.unread_count or 0,
                    date=dialog.date,
                    kind=kind,
                    can_send=_can_send(entity, kind),
                )
            )
        return dialogs

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[ChatMessage]:
        messages = []
        async for message in self._client.iter_messages(
            chat_id,
            limit=limit,
            offset_id=before_id or 0,
        ):
            messages.append(await self._to_message(message, chat_id))
        # Telethon fetches newest first; the UI reads top to bottom.
        messages.reverse()
        return messages

    async def send_message(self, chat_id: int, text: str) -> ChatMessage:
        message = await self._client.send_message(
            chat_id,
            text,
            parse_mode=None,
            link_preview=False,
        )
        return await self._to_message(message, chat_id)

    async def mark_read(self, chat_id: int) -> None:
        await self._client.send_read_acknowledge(chat_id)

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def _to_message(
        self,
        message: Any,
        chat_id: int,
        chat_title: str = "",
    ) -> ChatMessage:
        outgoing = bool(getattr(message, "out", False))
        if outgoing:
            sender_name = "我"
        else:
            sender = getattr(message, "sender", None)
            if sender is None and hasattr(message, "get_sender"):
                try:
                    sender = await message.get_sender()
                except Exception:
                    # Deleted accounts and inaccessible entities still have messages.
                    sender = None
            sender_name = getattr(message, "post_author", None) or _display_name(
                sender, str(getattr(message, "sender_id", None) or "未知用户")
            )
        text = getattr(message, "raw_text", None)
        if text is None:
            text = getattr(message, "message", "") or ""
        return ChatMessage(
            id=message.id,
            chat_id=chat_id,
            sender=sender_name,
            text=text,
            date=getattr(message, "date", None) or datetime.now(UTC),
            outgoing=outgoing,
            media=_media_description(message),
            chat_title=chat_title or self._chat_titles.get(chat_id, ""),
        )

    async def _on_new_message(self, event: Any) -> None:
        handler = self._handler
        if handler is None or event.chat_id is None:
            return
        try:
            chat_id = event.chat_id
            title = self._chat_titles.get(chat_id, "")
            if not title:
                try:
                    chat = await event.get_chat()
                    title = _display_name(chat, str(chat_id))
                    self._chat_titles[chat_id] = title
                except Exception:
                    title = str(chat_id)
            await handler(await self._to_message(event.message, chat_id, title))
        except Exception as exc:
            # UI failures must not break Telethon's update loop; avoid logging message text.
            _log.warning("New-message callback failed (%s)", type(exc).__name__)
