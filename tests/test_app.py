import asyncio

from textual.widgets import Input, Static

from telegram_tui.app import HelpScreen, LoginScreen, MessageBubble, TelegramApp
from telegram_tui.demo import DemoBackend


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_send_literal_text_once_and_shutdown_cleanly():
    backend = DemoBackend(reply_delay=60)
    app = TelegramApp(backend)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        assert app.ready and app.selected_chat == 1001
        composer = app.query_one("#composer", Input)
        text = "  [bold]literal[/bold] **plain** <tag>  "
        composer.value = text
        await pilot.press("enter")
        await settle(app, pilot)
        matching = [m for m in app.messages[1001] if "[bold]literal" in m.text]
        assert len(matching) == 1
        assert matching[0].text == text
        assert composer.value == ""
        assert not composer.disabled
        bubble = list(app.query(MessageBubble))[-1]
        assert text in bubble.render().plain
    assert not backend._connected
    assert not backend._tasks


async def test_search_switch_chat_and_preserve_drafts():
    app = TelegramApp(DemoBackend(reply_delay=60))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        composer = app.query_one("#composer", Input)
        composer.value = "尚未发送的草稿"
        await pilot.press("ctrl+k")
        assert app.focused.id == "search"
        app.query_one("#search", Input).value = "MIA CHEN"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.selected_chat == 1002
        assert composer.value == ""
        composer.value = "another draft"
        app.open_chat(1001)
        await settle(app, pilot)
        assert composer.value == "尚未发送的草稿"
        app.open_chat(1002)
        await settle(app, pilot)
        assert composer.value == "another draft"


async def test_realtime_unread_and_read_markers():
    backend = DemoBackend(reply_delay=60)
    app = TelegramApp(backend)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        assert app.dialogs[1001].unread == 0
        old_unread = app.dialogs[1002].unread
        incoming = await backend.inject_message(1002, "实时新消息")
        await settle(app, pilot)
        assert app.selected_chat == 1001
        assert app.dialogs[1002].unread == old_unread + 1
        await backend._emit(incoming)
        await settle(app, pilot)
        assert app.dialogs[1002].unread == old_unread + 1
        app.open_chat(1002)
        await settle(app, pilot)
        assert app.dialogs[1002].unread == 0
        assert sum(m.id == incoming.id for m in app.messages[1002]) == 1
        await backend.inject_message(1002, "当前会话的新消息")
        await settle(app, pilot)
        assert app.messages[1002][-1].text == "当前会话的新消息"
        assert app.dialogs[1002].unread == 0


async def test_history_pagination_deduplication_and_read_only_channel():
    app = TelegramApp(DemoBackend())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert len(app.messages[1001]) == 50
        assert app.query_one("#composer", Input).region.bottom < 24
        await pilot.press("ctrl+l")
        await settle(app, pilot)
        assert len(app.messages[1001]) == 62
        assert len({m.id for m in app.messages[1001]}) == 62
        assert 1001 in app.history_exhausted
        app.open_chat(1005)
        await settle(app, pilot)
        assert app.query_one("#composer", Input).disabled
        assert app.query_one("#composer", Input).placeholder == "此频道只读"
        await pilot.press("f1")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert not isinstance(app.screen, HelpScreen)


class SlowSendBackend(DemoBackend):
    def __init__(self, *, fail=False):
        super().__init__(reply_delay=60)
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.fail = fail

    async def send_message(self, chat_id, text):
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise ConnectionError("private network details")
        return await super().send_message(chat_id, text)


async def test_inflight_send_stays_in_original_chat_when_switching():
    backend = SlowSendBackend()
    app = TelegramApp(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        app.query_one("#composer", Input).value = "发给原会话"
        await pilot.press("enter")
        await asyncio.wait_for(backend.started.wait(), 1)
        app.open_chat(1002)
        await pilot.pause()
        app.query_one("#composer", Input).value = "新会话草稿"
        backend.release.set()
        await settle(app, pilot)
        assert app.selected_chat == 1002
        assert app.query_one("#composer", Input).value == "新会话草稿"
        assert app.messages[1001][-1].text == "发给原会话"
        assert all(m.text != "发给原会话" for m in app.messages[1002])
        app.open_chat(1001)
        await settle(app, pilot)
        assert app.query_one("#composer", Input).value == ""


async def test_failed_send_retains_draft_and_allows_retry():
    backend = SlowSendBackend(fail=True)
    backend.release.set()
    app = TelegramApp(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        composer = app.query_one("#composer", Input)
        composer.value = "保留这条消息"
        await pilot.press("enter")
        await settle(app, pilot)
        assert composer.value == "保留这条消息"
        assert not composer.disabled
        assert not app.sending
        backend.fail = False
        await pilot.press("enter")
        await settle(app, pilot)
        assert composer.value == ""


class LoginBackend(DemoBackend):
    is_demo = False

    def __init__(self):
        super().__init__()
        self.authorized = False
        self.phone = ""
        self.password = ""

    async def connect(self):
        await super().connect()
        return self.authorized

    async def request_code(self, phone):
        self.phone = phone

    async def sign_in(self, code):
        assert code == "12345"
        return False

    async def sign_in_password(self, password):
        self.password = password
        self.authorized = True


class TransportTaskLoginBackend(LoginBackend):
    """Model Telethon's transport tasks starting before its connected flag is set."""

    def __init__(self):
        super().__init__()
        self.transport_running = False

    async def connect(self):
        async def start_transport():
            return self._connected

        transport_task = asyncio.create_task(start_transport())
        authorized = await super().connect()
        self.transport_running = await transport_task
        if not self.transport_running:
            raise ConnectionError("transport task started before connection was ready")
        return authorized


async def test_production_runner_starts_transport_and_displays_phone_login():
    # run_test bypasses run_async's task factory setup and cannot catch this race.
    backend = TransportTaskLoginBackend()
    app = TelegramApp(backend)
    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()

    async def check_login(pilot):
        try:
            await asyncio.wait_for(settle(app, pilot), timeout=3)
            assert backend.transport_running
            assert isinstance(app.screen, LoginScreen)
            assert app.screen.stage == "phone"
            assert app.screen.query_one("#auth-value", Input).display
        finally:
            app.exit()

    try:
        await asyncio.wait_for(
            app.run_async(headless=True, size=(100, 30), auto_pilot=check_login), timeout=5
        )
    finally:
        loop.set_task_factory(original_factory)
    assert not backend._connected


async def test_production_runner_loads_demo_and_disconnects():
    backend = DemoBackend(reply_delay=60)
    app = TelegramApp(backend)
    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()

    async def check_demo(pilot):
        try:
            await asyncio.wait_for(settle(app, pilot), timeout=3)
            assert app.ready and app.selected_chat == 1001
            assert "演示" in app.query_one("#connection", Static).render().plain
        finally:
            app.exit()

    try:
        await asyncio.wait_for(
            app.run_async(headless=True, size=(100, 30), auto_pilot=check_demo), timeout=5
        )
    finally:
        loop.set_task_factory(original_factory)
    assert not backend._connected


async def test_login_phone_code_two_factor_without_real_network():
    backend = LoginBackend()
    app = TelegramApp(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, LoginScreen) and screen.stage == "phone"
        field = screen.query_one("#auth-value", Input)
        field.value = "invalid"
        await pilot.press("enter")
        await settle(app, pilot)
        assert screen.stage == "phone"
        assert "+" in str(screen.query_one("#login-error", Static).render())
        field.value = "+8613800000000"
        await pilot.press("enter")
        await settle(app, pilot)
        assert screen.stage == "code"
        assert backend.phone == "+8613800000000"
        field.value = "12345"
        await pilot.press("enter")
        await settle(app, pilot)
        assert screen.stage == "password"
        assert field.password
        field.value = " password with spaces "
        await pilot.press("enter")
        await settle(app, pilot)
        assert backend.password == " password with spaces "
        assert app.ready and app.selected_chat == 1001
        assert not isinstance(app.screen, LoginScreen)


async def test_initial_setup_screen_and_narrow_terminal():
    app = TelegramApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert isinstance(app.screen, LoginScreen)
        assert app.screen.stage == "credentials"
        assert app.screen.query_one("#api-hash", Input).password
        assert not app.ready
        assert app.screen.query_one("#login-submit").region.bottom <= 24
