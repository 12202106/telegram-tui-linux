"""Small, UI-independent data types shared by both backends."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class User:
    id: int
    name: str
    username: str = ""


@dataclass(slots=True)
class Dialog:
    id: int
    name: str
    preview: str = ""
    unread: int = 0
    date: datetime | None = None
    kind: str = "private"
    can_send: bool = True


@dataclass(slots=True)
class ChatMessage:
    id: int
    chat_id: int
    sender: str
    text: str
    date: datetime
    outgoing: bool = False
    media: str | None = None
    chat_title: str = ""


MessageHandler = Callable[[ChatMessage], Awaitable[None]]


class Backend(Protocol):
    is_demo: bool

    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    async def request_code(self, phone: str) -> None: ...
    async def sign_in(self, code: str) -> bool: ...
    async def sign_in_password(self, password: str) -> None: ...
    async def get_me(self) -> User: ...
    async def get_dialogs(self, limit: int = 200) -> list[Dialog]: ...
    async def get_messages(
        self, chat_id: int, limit: int = 50, before_id: int | None = None
    ) -> list[ChatMessage]: ...
    async def send_message(self, chat_id: int, text: str) -> ChatMessage: ...
    async def mark_read(self, chat_id: int) -> None: ...
    def set_message_handler(self, handler: MessageHandler) -> None: ...
