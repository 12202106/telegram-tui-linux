"""Textual interface. All network I/O runs in cancellable async workers."""

from __future__ import annotations

import asyncio
from typing import cast

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .backend import TelegramBackend, describe_error
from .config import Settings
from .models import Backend, ChatMessage, Dialog


class IncomingMessage(Message):
    def __init__(self, message: ChatMessage) -> None:
        super().__init__()
        self.message = message


class LoginValidationError(ValueError):
    """A local validation message that is safe to show verbatim."""


class MessageBubble(Static):
    def __init__(self, message: ChatMessage) -> None:
        self.message = message
        super().__init__(classes="bubble outgoing" if message.outgoing else "bubble")

    def render(self) -> Text:
        message = self.message
        content = Text()
        content.append("你" if message.outgoing else message.sender, style="bold #66d9c5")
        content.append(f"   {message.date.astimezone():%H:%M}", style="#8393a9")
        if message.outgoing:
            content.append("  · 已发送", style="#8393a9")
        content.append("\n")
        if message.text:
            content.append(message.text, style="#e3eaf4")
        if message.media:
            if message.text:
                content.append("\n")
            content.append(f"▧ {message.media}", style="italic #a5b5cc")
        if not message.text and not message.media:
            content.append("[服务消息]", style="#8393a9")
        return content


class HelpScreen(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "关闭"), ("f1", "dismiss", "关闭")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-card"):
            yield Label("键盘就是你的主场", classes="modal-title")
            yield Static(
                "Ctrl+K       搜索会话\n"
                "↑ / ↓        在会话列表中移动\n"
                "Enter        打开会话 / 发送消息\n"
                "Ctrl+N / P   下一条 / 上一条会话\n"
                "Ctrl+L       加载更早的消息\n"
                "Ctrl+R       刷新会话与当前消息\n"
                "Escape       返回消息输入框\n"
                "Tab          在控件间移动\n"
                "Ctrl+Q       退出\n\n"
                "滚轮或聚焦消息区后用 PgUp / PgDn 浏览历史。\n"
                "打开会话会标记已读；附件目前显示类型与文件名。",
                markup=False,
            )
            yield Button("知道了", id="close-help", variant="primary")

    @on(Button.Pressed, "#close-help")
    def close_help(self) -> None:
        self.dismiss(None)


class LoginScreen(ModalScreen[bool]):
    """Explicit credential → phone → code → optional password state machine."""

    def __init__(self, stage: str = "credentials", error: str = "") -> None:
        super().__init__()
        self.stage = stage
        self.error = error
        self.busy = False

    @property
    def client_app(self) -> TelegramApp:
        return cast(TelegramApp, self.app)

    def compose(self) -> ComposeResult:
        settings = self.client_app.settings
        with Vertical(id="login-card"):
            yield Label("↗  TELEGRAM / TERMINAL", classes="modal-eyebrow")
            yield Label("在终端，继续对话。", classes="modal-title")
            yield Static("", id="login-hint", markup=False)
            yield Input(
                str(settings.api_id or ""), placeholder="API ID", id="api-id", type="integer"
            )
            yield Input(settings.api_hash, placeholder="API Hash", id="api-hash", password=True)
            yield Input(placeholder="手机号（含国家区号，例如 +86…）", id="auth-value")
            yield Static(self.error, id="login-error", markup=False)
            yield Button("继续", id="login-submit", variant="primary")
            yield Button("重新开始", id="login-back")
            yield Static("凭据仅用于连接 Telegram；登录会话保存在本机。", classes="muted")

    def on_mount(self) -> None:
        self.update_stage()

    def update_stage(self) -> None:
        credentials = self.stage == "credentials"
        self.query_one("#api-id", Input).display = credentials
        self.query_one("#api-hash", Input).display = credentials
        value = self.query_one("#auth-value", Input)
        value.display = self.stage in {"phone", "code", "password"}
        value.password = self.stage in {"code", "password"}
        value.value = ""
        hints = {
            "credentials": (
                "从 my.telegram.org/apps 获取 API ID 与 API Hash。\n"
                "也可以通过环境变量配置，下次无需重新输入。"
            ),
            "connect": "连接暂时中断。检查网络或代理设置，然后重试。",
            "phone": "使用自己的 Telegram 账号登录。请输入含国家区号的手机号。",
            "code": "请输入 Telegram 发给你的验证码（通常在 Telegram 应用内）。",
            "password": "此账号启用了两步验证。请输入 Telegram 云密码。",
        }
        placeholders = {
            "phone": "手机号，例如 +8613800000000",
            "code": "Telegram 验证码",
            "password": "两步验证密码",
        }
        self.query_one("#login-hint", Static).update(hints[self.stage])
        value.placeholder = placeholders.get(self.stage, "")
        self.query_one("#login-submit", Button).label = (
            "重新连接" if self.stage == "connect" else "继续 →"
        )
        self.query_one("#login-back", Button).display = self.stage != "credentials"
        if credentials:
            self.query_one("#api-id", Input).focus()
        elif value.display:
            value.focus()
        else:
            self.query_one("#login-submit", Button).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#login-submit")
    def submit(self) -> None:
        if not self.busy:
            self.perform_login()

    @on(Button.Pressed, "#login-back")
    def restart(self) -> None:
        if not self.busy:
            self.stage = "phone" if self.stage in {"code", "password"} else "credentials"
            self.query_one("#login-error", Static).update("")
            self.update_stage()

    @work(group="login", exclusive=True)
    async def perform_login(self) -> None:
        self.busy = True
        submit = self.query_one("#login-submit", Button)
        submit.disabled = True
        self.query_one("#login-back", Button).disabled = True
        self.query_one("#login-error", Static).update("")
        app = self.client_app
        try:
            value = self.query_one("#auth-value", Input).value.strip()
            authorized = False
            if self.stage == "credentials":
                api_id = self.query_one("#api-id", Input).value.strip()
                api_hash = self.query_one("#api-hash", Input).value.strip()
                if not api_id.isdecimal() or int(api_id) <= 0:
                    raise LoginValidationError("请输入有效的 API ID（正整数）。")
                if len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
                    raise LoginValidationError("API Hash 应为 32 位十六进制字符。")
                if app.backend:
                    await app.backend.disconnect()
                app.backend = TelegramBackend(
                    int(api_id), api_hash, app.settings.session_path, proxy=app.settings.proxy
                )
                # Retry the connection with this same session if networking fails.
                self.stage = "connect"
                self.query_one("#api-hash", Input).value = ""
                authorized = await app.backend.connect()
                self.stage = "phone"
            elif self.stage == "connect":
                assert app.backend is not None
                authorized = await app.backend.connect()
                self.stage = "phone"
            elif self.stage == "phone":
                if not value.startswith("+") or not value[1:].isdigit():
                    raise LoginValidationError("手机号需包含 + 和国家区号，例如 +8613800000000。")
                assert app.backend is not None
                await app.backend.request_code(value)
                self.stage = "code"
            elif self.stage == "code":
                if not value:
                    raise LoginValidationError("请输入验证码。")
                assert app.backend is not None
                authorized = await app.backend.sign_in(value.replace(" ", ""))
                if not authorized:
                    self.stage = "password"
            elif self.stage == "password":
                # Passwords may intentionally contain leading/trailing whitespace.
                password = self.query_one("#auth-value", Input).value
                if not password:
                    raise LoginValidationError("请输入两步验证密码。")
                assert app.backend is not None
                await app.backend.sign_in_password(password)
                authorized = True
            if authorized:
                self.query_one("#auth-value", Input).value = ""
                self.dismiss(True)
                return
            self.update_stage()
        except Exception as exc:
            self.query_one("#login-error", Static).update(
                str(exc) if isinstance(exc, LoginValidationError) else describe_error(exc)
            )
            if self.stage == "connect":
                self.update_stage()
        finally:
            self.busy = False
            submit.disabled = False
            self.query_one("#login-back", Button).disabled = False


class TelegramApp(App[None]):
    TITLE = "Telegram TUI"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+k", "search", "搜索", priority=True),
        Binding("ctrl+n", "next_chat", "下一会话", show=False, priority=True),
        Binding("ctrl+p", "previous_chat", "上一会话", show=False, priority=True),
        Binding("ctrl+l", "older", "更早消息", priority=True),
        Binding("ctrl+r", "refresh", "刷新", priority=True),
        Binding("escape", "composer", "输入", show=False),
        Binding("f1", "help", "帮助"),
        Binding("ctrl+q", "quit", "退出", priority=True),
    ]

    def __init__(self, backend: Backend | None = None, settings: Settings | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.settings = settings or Settings()
        self.dialogs: dict[int, Dialog] = {}
        self.messages: dict[int, list[ChatMessage]] = {}
        self.drafts: dict[int, str] = {}
        self.selected_chat: int | None = None
        self.ready = False
        self.sending: set[int] = set()
        self.loading_older: set[int] = set()
        self.history_exhausted: set[int] = set()
        self._render_lock = asyncio.Lock()
        self._closing = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(
                Text.assemble(("↗  TELEGRAM", "bold #66d9c5"), ("  / terminal", "#8393a9")),
                id="brand",
            )
            yield Static("正在启动…", id="connection", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                with Horizontal(id="sidebar-heading"):
                    yield Label("消息", id="inbox-title")
                    yield Label("0", id="dialog-count")
                yield Input(placeholder="搜索会话  Ctrl+K", id="search", select_on_focus=False)
                yield OptionList(id="dialogs")
                yield Static("YOUR CONVERSATIONS, LESS NOISE.", id="sidebar-note")
            with Vertical(id="conversation"):
                with Vertical(id="chat-heading"):
                    yield Static("欢迎使用 Telegram TUI", id="chat-title", markup=False)
                    yield Static("让对话留在终端里。", id="chat-subtitle", markup=False)
                yield VerticalScroll(
                    Static("↗\n\n选择一段对话，开始聊天。", id="empty-state", markup=False),
                    id="messages",
                )
                with Horizontal(id="composer-row"):
                    yield Input(
                        placeholder="输入消息…", id="composer", disabled=True, select_on_focus=False
                    )
                    yield Button("发送 ↵", id="send", variant="primary", disabled=True)
                yield Static("Enter 发送  ·  Ctrl+L 更早消息  ·  F1 快捷键", id="compose-hint")
        yield Footer()

    def on_mount(self) -> None:
        # Textual's production runner enables eager tasks on Python 3.12+.
        # Telethon starts its send/receive tasks before marking the transport
        # connected; eager execution makes both tasks exit before that flag is
        # set. Use deferred scheduling for the application's network workers.
        loop = asyncio.get_running_loop()
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is not None and loop.get_task_factory() is eager_factory:
            loop.set_task_factory(None)
        self.theme = "textual-dark"
        self.initialize()

    @work(group="initialize", exclusive=True)
    async def initialize(self) -> None:
        if self.backend is None and self.settings.has_credentials:
            try:
                self.backend = TelegramBackend(
                    cast(int, self.settings.api_id),
                    self.settings.api_hash,
                    self.settings.session_path,
                    proxy=self.settings.proxy,
                )
            except Exception as exc:
                self.push_screen(LoginScreen(error=describe_error(exc)), self._login_finished)
                return
        if self.backend is None:
            self.push_screen(LoginScreen(), self._login_finished)
            return
        if not self.backend.is_demo:
            self.query_one("#connection", Static).update("正在连接 Telegram…")
        try:
            if await self.backend.connect():
                await self.load_account()
            else:
                self.push_screen(LoginScreen("phone"), self._login_finished)
        except Exception as exc:
            self.query_one("#connection", Static).update("连接失败 · 请重试")
            self.push_screen(LoginScreen("connect", describe_error(exc)), self._login_finished)

    def _login_finished(self, authorized: bool | None) -> None:
        if authorized:
            self.run_worker(self.load_account(), group="account", exclusive=True)

    async def load_account(self) -> None:
        assert self.backend is not None
        try:
            self.backend.set_message_handler(self.receive_message)
            user = await self.backend.get_me()
            dialogs = await self.backend.get_dialogs()
            self.dialogs = {dialog.id: dialog for dialog in dialogs}
            self.ready = True
            mode = "演示 · 本地数据" if self.backend.is_demo else "Telegram"
            self.query_one("#connection", Static).update(f"●  {mode}  /  {user.name}")
            self.render_dialogs()
            if dialogs:
                self.open_chat(dialogs[0].id)
            else:
                self.query_one("#chat-subtitle", Static).update("暂无会话。Ctrl+R 可以重新加载。")
        except Exception as exc:
            self.notify_error(exc)
            self.query_one("#connection", Static).update("加载失败 · Ctrl+R 重试")

    def render_dialogs(self) -> None:
        query = self.query_one("#search", Input).value.strip().casefold()
        options = self.query_one("#dialogs", OptionList)
        highlighted_id = None
        if options.highlighted is not None and options.option_count:
            highlighted_id = options.get_option_at_index(options.highlighted).id
        visible = [d for d in self.dialogs.values() if query in d.name.casefold()]
        visible.sort(key=lambda d: d.date.timestamp() if d.date else 0, reverse=True)
        options.clear_options()
        row_width = max(12, options.content_size.width - 2)
        for dialog in visible:
            text = Text(no_wrap=True, overflow="ellipsis")
            icons = {"group": "◈", "channel": "◉", "saved": "◇", "private": "●"}
            text.append(f"{icons.get(dialog.kind, '●')} ", style="#66d9c5")
            text.append(dialog.name, style="bold #e3eaf4")
            if dialog.unread:
                text.append(f"  {dialog.unread}", style="bold #66d9c5")
            text.truncate(row_width, overflow="ellipsis")
            preview = Text(" ".join(dialog.preview.split()), style="#8393a9")
            preview.truncate(row_width - 2, overflow="ellipsis")
            text.append("\n  ")
            text.append_text(preview)
            options.add_option(Option(text, id=str(dialog.id)))
        ids = [str(d.id) for d in visible]
        preferred = highlighted_id or str(self.selected_chat)
        if preferred in ids:
            options.highlighted = ids.index(preferred)
        elif visible:
            options.highlighted = 0
        self.query_one("#dialog-count", Label).update(str(len(visible)))
        self.query_one("#sidebar-note", Static).update(
            "没有匹配的会话" if not visible else "YOUR CONVERSATIONS, LESS NOISE."
        )

    @on(Input.Changed, "#search")
    def filter_dialogs(self) -> None:
        self.render_dialogs()

    @on(Input.Submitted, "#search")
    def choose_search_result(self) -> None:
        options = self.query_one("#dialogs", OptionList)
        if options.highlighted is not None and options.option_count:
            self.open_chat(int(cast(str, options.get_option_at_index(options.highlighted).id)))

    @on(OptionList.OptionSelected, "#dialogs")
    def select_dialog(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.open_chat(int(event.option.id))

    def open_chat(self, chat_id: int) -> None:
        if not self.ready or chat_id not in self.dialogs:
            return
        composer = self.query_one("#composer", Input)
        if self.selected_chat is not None:
            self.drafts[self.selected_chat] = composer.value
        self.selected_chat = chat_id
        dialog = self.dialogs[chat_id]
        self.query_one("#chat-title", Static).update(dialog.name)
        kinds = {"group": "群组", "channel": "频道", "private": "私聊", "saved": "收藏夹"}
        mode = "  ·  演示模式" if self.backend and self.backend.is_demo else ""
        self.query_one("#chat-subtitle", Static).update(
            f"{kinds.get(dialog.kind, '会话')}{mode}  ·  正在加载消息…"
        )
        composer.value = self.drafts.get(chat_id, "")
        self.update_composer()
        if not composer.disabled:
            composer.focus()
        self.load_history(chat_id)

    def update_composer(self) -> None:
        dialog = self.dialogs.get(self.selected_chat) if self.selected_chat is not None else None
        blocked = not dialog or not dialog.can_send or dialog.id in self.sending
        composer = self.query_one("#composer", Input)
        composer.disabled = bool(blocked)
        self.query_one("#send", Button).disabled = bool(blocked)
        composer.placeholder = (
            "此频道只读"
            if dialog and not dialog.can_send
            else "正在发送…"
            if dialog and dialog.id in self.sending
            else "输入消息…"
        )

    @work(group="history", exclusive=True)
    async def load_history(self, chat_id: int) -> None:
        assert self.backend is not None
        await self.render_messages(bottom=True)
        try:
            history = await self.backend.get_messages(chat_id)
            self.merge_messages(chat_id, history)
            if len(history) < 50:
                self.history_exhausted.add(chat_id)
            if self.selected_chat == chat_id:
                await self.render_messages(bottom=True)
                await self.read_chat(chat_id)
        except Exception as exc:
            self.notify_error(exc)
            if self.selected_chat == chat_id:
                self.query_one("#chat-subtitle", Static).update("消息加载失败 · Ctrl+R 重试")

    def merge_messages(self, chat_id: int, incoming: list[ChatMessage]) -> None:
        by_id = {message.id: message for message in self.messages.get(chat_id, [])}
        by_id.update({message.id: message for message in incoming})
        self.messages[chat_id] = sorted(by_id.values(), key=lambda m: (m.date, m.id))

    async def render_messages(self, *, bottom: bool = False, preserve: bool = False) -> None:
        async with self._render_lock:
            chat_id = self.selected_chat
            if chat_id is None:
                return
            container = self.query_one("#messages", VerticalScroll)
            old_y, old_max = container.scroll_y, container.max_scroll_y
            was_bottom = container.is_vertical_scroll_end
            await container.remove_children()
            messages = list(self.messages.get(chat_id, []))
            widgets: list[Static] = []
            previous_day = ""
            for message in messages:
                day = message.date.astimezone().strftime("%Y 年 %m 月 %d 日")
                if day != previous_day:
                    widgets.append(Static(day, classes="day-divider", markup=False))
                    previous_day = day
                widgets.append(MessageBubble(message))
            if not widgets:
                widgets.append(Static("还没有消息，打个招呼吧。", classes="empty-chat"))
            await container.mount(*widgets)
            if chat_id != self.selected_chat:
                return
            if preserve:

                def restore() -> None:
                    container.scroll_to(y=old_y + container.max_scroll_y - old_max, animate=False)

                self.call_after_refresh(restore)
            elif bottom or was_bottom:
                container.scroll_end(animate=False)
            else:
                self.call_after_refresh(container.scroll_to, y=old_y, animate=False)
            dialog = self.dialogs[chat_id]
            kind = {"group": "群组", "channel": "频道", "saved": "收藏夹"}.get(dialog.kind, "私聊")
            mode = "  ·  演示模式" if self.backend and self.backend.is_demo else ""
            self.query_one("#chat-subtitle", Static).update(
                f"{kind}{mode}  ·  {len(messages)} 条消息  ·  Ctrl+L 加载更早"
            )

    async def read_chat(self, chat_id: int) -> None:
        assert self.backend is not None
        if self.selected_chat != chat_id:
            return
        try:
            await self.backend.mark_read(chat_id)
            if chat_id in self.dialogs:
                self.dialogs[chat_id].unread = 0
                self.render_dialogs()
        except Exception as exc:
            self.notify_error(exc)

    async def receive_message(self, message: ChatMessage) -> None:
        if not self._closing:
            self.post_message(IncomingMessage(message))

    async def on_incoming_message(self, event: IncomingMessage) -> None:
        await self.apply_message(event.message)

    async def apply_message(self, message: ChatMessage) -> None:
        existing = self.messages.get(message.chat_id, [])
        duplicate = any(item.id == message.id for item in existing)
        self.merge_messages(message.chat_id, [message])
        dialog = self.dialogs.get(message.chat_id)
        if dialog is None:
            dialog = Dialog(message.chat_id, message.chat_title or message.sender)
            self.dialogs[message.chat_id] = dialog
        if not dialog.date or message.date >= dialog.date:
            dialog.preview = message.text or message.media or "[服务消息]"
            dialog.date = message.date
        if not duplicate and not message.outgoing and self.selected_chat != message.chat_id:
            dialog.unread += 1
        self.render_dialogs()
        if self.selected_chat == message.chat_id:
            await self.render_messages(bottom=message.outgoing)
            if not message.outgoing:
                self.run_worker(self.read_chat(message.chat_id), group="read", exclusive=True)

    @on(Input.Submitted, "#composer")
    @on(Button.Pressed, "#send")
    def submit_message(self) -> None:
        composer = self.query_one("#composer", Input)
        if self.selected_chat is None or composer.disabled or not composer.value.strip():
            return
        if len(composer.value.encode("utf-16-le")) // 2 > 4096:
            self.notify("消息过长，请缩短到 4096 字符以内。", severity="warning")
            return
        chat_id, text = self.selected_chat, composer.value
        self.drafts[chat_id] = text
        self.sending.add(chat_id)
        self.update_composer()
        self.send_text(chat_id, text)

    @work(group="send")
    async def send_text(self, chat_id: int, text: str) -> None:
        assert self.backend is not None
        try:
            sent = await self.backend.send_message(chat_id, text)
            await self.apply_message(sent)
            if self.drafts.get(chat_id) == text:
                self.drafts[chat_id] = ""
            if self.selected_chat == chat_id and self.query_one("#composer", Input).value == text:
                self.query_one("#composer", Input).value = ""
        except Exception as exc:
            self.notify_error(exc)
        finally:
            self.sending.discard(chat_id)
            self.update_composer()
            if self.selected_chat == chat_id:
                self.query_one("#composer", Input).focus()

    def action_search(self) -> None:
        if self.ready and not isinstance(self.screen, ModalScreen):
            self.query_one("#search", Input).focus()

    def action_composer(self) -> None:
        if self.ready and not isinstance(self.screen, ModalScreen):
            self.query_one("#composer", Input).focus()

    def move_chat(self, direction: int) -> None:
        if not self.ready or isinstance(self.screen, ModalScreen):
            return
        options = self.query_one("#dialogs", OptionList)
        if not options.option_count:
            return
        ids = [
            int(cast(str, options.get_option_at_index(i).id)) for i in range(options.option_count)
        ]
        current = ids.index(self.selected_chat) if self.selected_chat in ids else -1
        index = (current + direction) % len(ids)
        options.highlighted = index
        self.open_chat(ids[index])

    def action_next_chat(self) -> None:
        self.move_chat(1)

    def action_previous_chat(self) -> None:
        self.move_chat(-1)

    def action_help(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.push_screen(HelpScreen())

    def action_older(self) -> None:
        if (
            self.ready
            and self.selected_chat is not None
            and not isinstance(self.screen, ModalScreen)
        ):
            self.load_older(self.selected_chat)

    @work(group="older")
    async def load_older(self, chat_id: int) -> None:
        assert self.backend is not None
        if chat_id in self.loading_older:
            return
        if chat_id in self.history_exhausted:
            self.notify("已经到达这段对话的起点。")
            return
        current = self.messages.get(chat_id, [])
        if not current:
            return
        self.loading_older.add(chat_id)
        try:
            older = await self.backend.get_messages(chat_id, before_id=min(m.id for m in current))
            self.merge_messages(chat_id, older)
            if len(older) < 50:
                self.history_exhausted.add(chat_id)
            if self.selected_chat == chat_id:
                await self.render_messages(preserve=True)
            if not older:
                self.notify("已经到达这段对话的起点。")
        except Exception as exc:
            self.notify_error(exc)
        finally:
            self.loading_older.discard(chat_id)

    def action_refresh(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if not self.ready:
            self.run_worker(self.load_account(), group="account", exclusive=True)
        else:
            self.refresh_account()

    @work(group="refresh", exclusive=True)
    async def refresh_account(self) -> None:
        assert self.backend is not None
        try:
            dialogs = await self.backend.get_dialogs()
            self.dialogs = {d.id: d for d in dialogs}
            self.render_dialogs()
            if self.selected_chat in self.dialogs:
                self.load_history(cast(int, self.selected_chat))
                self.update_composer()
            elif dialogs:
                self.open_chat(dialogs[0].id)
            self.notify("会话已刷新。", timeout=2)
        except Exception as exc:
            self.notify_error(exc)

    def notify_error(self, exc: Exception) -> None:
        self.notify(describe_error(exc), title="操作未完成", severity="error", timeout=7)

    async def on_unmount(self) -> None:
        self._closing = True
        if self.backend:
            await self.backend.disconnect()
