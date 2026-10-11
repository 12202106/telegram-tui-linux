"""A deterministic, offline Telegram backend for exploring the interface.

``get_messages(chat_id, limit=50, before_id=None)`` returns the latest matching
messages in chronological order. ``before_id`` is an exclusive message cursor.
``inject_message(chat_id, text, sender="演示助手")`` simulates an incoming event.
No credentials, Telegram connection, or network access are used by this module.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from telegram_tui.models import ChatMessage, Dialog, MessageHandler, User


class DemoBackend:
    """An in-memory backend with delayed replies and real message events."""

    is_demo = True

    def __init__(self, *, reply_delay: float = 1.2) -> None:
        self._me = User(id=1, name="林间", username="terminal_user")
        self._handler: MessageHandler | None = None
        self._connected = False
        self._reply_delay = reply_delay
        self._tasks: set[asyncio.Task[None]] = set()
        self._dialogs: dict[int, Dialog] = {}
        self._messages: dict[int, list[ChatMessage]] = {}
        self._seed()

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def disconnect(self) -> None:
        """Stop all pending replies before returning."""
        self._connected = False
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def request_code(self, phone: str) -> None:
        """Demo mode is already authorized; no code is sent."""

    async def sign_in(self, code: str) -> bool:
        return True

    async def sign_in_password(self, password: str) -> None:
        """Demo mode never needs a password."""

    async def get_me(self) -> User:
        return replace(self._me)

    async def get_dialogs(self, limit: int = 200) -> list[Dialog]:
        dialogs = sorted(self._dialogs.values(), key=lambda item: item.date, reverse=True)
        return [replace(dialog) for dialog in dialogs[: max(0, limit)]]

    async def get_messages(
        self, chat_id: int, limit: int = 50, before_id: int | None = None
    ) -> list[ChatMessage]:
        """Return at most ``limit`` messages, oldest first, before the cursor."""
        self._get_dialog(chat_id)
        if limit <= 0:
            return []
        messages = self._messages[chat_id]
        eligible = [message for message in messages if before_id is None or message.id < before_id]
        return [replace(message) for message in eligible[-limit:]]

    async def send_message(self, chat_id: int, text: str) -> ChatMessage:
        self._require_connection()
        dialog = self._get_dialog(chat_id)
        if not dialog.can_send:
            raise PermissionError("这个频道仅支持阅读。")
        message = self._append(chat_id, text, self._me.name, outgoing=True)
        await self._emit(message)
        # A handler may close the app while processing the outgoing event.
        if self._connected and dialog.kind != "saved":
            task = asyncio.create_task(self._reply(chat_id, message.text))
            self._tasks.add(task)
            task.add_done_callback(self._reply_finished)
        return replace(message)

    async def inject_message(
        self, chat_id: int, text: str, sender: str = "演示助手"
    ) -> ChatMessage:
        """Append an incoming message, update unread count, and emit its event."""
        self._require_connection()
        self._get_dialog(chat_id)
        message = self._append(chat_id, text, sender, outgoing=False)
        await self._emit(message)
        return replace(message)

    async def mark_read(self, chat_id: int) -> None:
        self._get_dialog(chat_id).unread = 0

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def _get_dialog(self, chat_id: int) -> Dialog:
        try:
            return self._dialogs[chat_id]
        except KeyError as error:
            raise ValueError(f"找不到会话：{chat_id}") from error

    def _require_connection(self) -> None:
        if not self._connected:
            raise ConnectionError("演示客户端尚未连接。")

    def _append(self, chat_id: int, text: str, sender: str, *, outgoing: bool) -> ChatMessage:
        if not text.strip():
            raise ValueError("消息不能为空。")
        dialog = self._get_dialog(chat_id)
        history = self._messages[chat_id]
        date = datetime.now(UTC)
        latest_activity = max(
            (item.date for item in self._dialogs.values() if item.date is not None),
            default=date,
        )
        date = max(date, latest_activity + timedelta(microseconds=1))
        message = ChatMessage(
            id=history[-1].id + 1 if history else 1,
            chat_id=chat_id,
            sender=sender,
            text=text,
            date=date,
            outgoing=outgoing,
            chat_title=dialog.name,
        )
        history.append(message)
        dialog.preview = text
        dialog.date = date
        if not outgoing:
            dialog.unread += 1
        return message

    async def _emit(self, message: ChatMessage) -> None:
        if self._handler is not None:
            await self._handler(replace(message))

    async def _reply(self, chat_id: int, text: str) -> None:
        await asyncio.sleep(self._reply_delay)
        if not self._connected:
            return
        dialog = self._get_dialog(chat_id)
        sender = "阿澈" if dialog.kind == "group" else dialog.name
        lowered = text.casefold()
        if any(word in lowered for word in ("hello", "hi", "你好", "嗨")):
            reply = "嗨，林间！欢迎回来。今天想做点什么？"
        elif any(word in lowered for word in ("theme", "主题", "颜色", "配色")):
            reply = "我也喜欢这套配色。暗一点的背景，留一点亮色给真正重要的消息。"
        elif any(word in lowered for word in ("python", "textual", "终端", "tui")):
            reply = "终端里也能有舒服的聊天体验。试试切换会话，再回来看看这条消息。"
        elif any(word in lowered for word in ("谢谢", "thanks", "thank you")):
            reply = "不客气，慢慢来。好东西总是从一个小想法开始的。"
        elif "?" in text or "？" in text:
            reply = "这个问题值得聊聊。先把想法记下来，我们一点点试。"
        else:
            reply = "收到啦。这里是离线演示，消息只留在这次会话里，放心试试吧。"
        await self.inject_message(chat_id, reply, sender=sender)

    def _reply_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        # Event handlers belong to the UI; a failed handler should not leave an
        # unobserved background-task exception during application shutdown.
        if not task.cancelled():
            task.exception()

    def _seed(self) -> None:
        base = datetime(2026, 10, 11, 10, 42, tzinfo=UTC)
        old_topics = [
            ("Mia", "Morning, terminal people. Coffee first, commits second."),
            ("阿澈", "今天想把通知做得安静一点，只在真正需要的时候出现。"),
            ("林间", "赞同。能只靠键盘完成的事情，就不用伸手找鼠标了。"),
            ("Noah", "A tiny detail: empty states deserve good copy too."),
            ("阿澈", "刚给自己的 dotfiles 整理了目录，终于找得到配置了。"),
            ("Mia", "Small tools, carefully made. That's the whole idea."),
            ("林间", "我把待办缩到三件了，今天应该能早点收工。"),
            ("Noah", "Does anyone else collect terminal color palettes?"),
            ("阿澈", "我的收藏夹里大概有二十套，常用的还是那两套。"),
            ("Mia", "I keep coming back to warm neutrals with a little green."),
            ("林间", "留白也很重要，一屏不需要塞满所有信息。"),
            ("阿澈", "有人试过在终端里写日记吗？最近想养成这个习惯。"),
            ("Noah", "Plain text, one file per day. Surprisingly nice."),
            ("Mia", "And a search command is all the navigation you need."),
            ("林间", "这个可以，下班前写三行应该不会太有压力。"),
            ("阿澈", "午饭回来修掉了一个很小但看着很难受的对齐问题。"),
            ("Noah", "The best kind of fix. Instantly feels calmer."),
            ("Mia", "Reminder: take a short walk before the next big refactor."),
            ("林间", "收到，去楼下买一杯冰美式。"),
            ("阿澈", "帮我带一杯，谢谢！"),
            ("Noah", "Today's playlist is just rain sounds and quiet jazz."),
            ("Mia", "That sounds like a very productive afternoon."),
            ("林间", "周末打算做一个很小的聊天客户端。"),
            ("阿澈", "终端版？这个我想试。"),
            ("林间", "对，左边是会话，右边是消息，底下留一个输入框。"),
            ("Noah", "Keep the compose box visible. Always."),
            ("Mia", "And please make unread counts easy to scan."),
            ("阿澈", "历史消息多的时候，向上翻应该能继续加载。"),
            ("林间", "记下了，也会给只读频道一个清楚的提示。"),
            ("Noah", "A good search field would make switching chats effortless."),
            ("Mia", "What should the default conversation look like?"),
            ("阿澈", "像我们现在这样，随意聊聊，不要太像产品说明书。"),
            ("林间", "哈哈，赞同。先让它像一个有人在用的地方。"),
            ("Noah", "A little personality goes a long way."),
            ("Mia", "I made a quick sketch with a narrow sidebar."),
            ("阿澈", "侧栏别太窄，中文名字也得放得下。"),
            ("林间", "时间放轻一点，消息内容是主角。"),
            ("Noah", "The hierarchy is starting to feel right."),
            ("Mia", "Can we keep links selectable as plain text?"),
            ("林间", "可以，消息里的格式也会按原文显示。"),
            ("阿澈", "很适合直接贴代码片段和命令。"),
            ("Noah", "One more thing: long messages should wrap nicely."),
            ("Mia", "Especially when someone writes a whole essay in chat."),
            ("阿澈", "你是在说我吗。"),
            ("林间", "没事，长消息也有它的容身之处。"),
            ("Noah", "We should give the demo a few older conversations too."),
            ("Mia", "A quiet saved-messages space would be lovely."),
            ("阿澈", "拿来记灵感，或者往电脑传一点小东西。"),
            ("林间", "再加一个只读频道，看看浏览公告的感觉。"),
            ("Noah", "Sounds like a weekend well spent."),
            ("Mia", "The first working version is the best milestone."),
            ("阿澈", "做完我们来一起试试。"),
        ]
        recent = [
            ("Mia", "A little corner of the internet, right in your terminal."),
            ("阿澈", "早上好，Terminal Club。今天的咖啡和代码都准备好了吗？"),
            ("Noah", "Coffee: ready. Inbox: almost. ☕"),
            ("林间", "刚把聊天搬进终端了，感觉工作台又清爽了一点。"),
            ("Mia", "Love it. Everything you need, a few keystrokes away."),
            ("阿澈", "这个绿色很舒服，有点旧显示器的味道。"),
            ("林间", "我最喜欢的是不用离开键盘，就能和大家保持联系。"),
            ("Noah", "Less switching windows, more staying in the flow."),
            ("Mia", "What's everyone building this weekend?"),
            ("阿澈", "先从一个小想法开始吧。林间，要不要分享一下你的？"),
        ]
        self._add_chat(1001, "Terminal Club", "group", old_topics + recent, base, unread=3)
        self._add_chat(
            1002,
            "Mia Chen",
            "private",
            [
                ("Mia Chen", "Hey! I finally finished the little reading app."),
                ("林间", "太好了，今晚可以试一下。"),
                ("Mia Chen", "No rush. Would love to hear what you think."),
                ("Mia Chen", "See you at the coffee shop tomorrow? ☕"),
            ],
            base - timedelta(minutes=8),
            unread=1,
        )
        self._add_chat(
            1003,
            "Saved Messages",
            "saved",
            [
                ("林间", "Small things, made with care."),
                ("林间", "周末清单\n• 整理书桌\n• 走一条没走过的路\n• 给小项目收个尾"),
                ("林间", "灵感：一个安静、轻巧、只靠键盘就能使用的聊天空间。"),
            ],
            base - timedelta(minutes=22),
        )
        self._add_chat(
            1004,
            "Design & Details",
            "group",
            [
                ("Yuki", "今天看到一句话：好的设计会给人喘息的空间。"),
                ("林间", "很喜欢，做界面的时候也该记住这一点。"),
                ("Oliver", "Whitespace is a feature."),
                ("Yuki", "新的配色方案放进共享文件夹了，晚点一起看看。"),
            ],
            base - timedelta(minutes=35),
            unread=2,
        )
        self._add_chat(
            1005,
            "Python Weekly",
            "channel",
            [
                ("Python Weekly", "This week: small Python projects with thoughtful interfaces."),
                (
                    "Python Weekly",
                    "Reading list: asyncio patterns, readable tests, and terminal apps.",
                ),
                ("Python Weekly", "Weekend idea: build something useful in under 200 lines."),
            ],
            base - timedelta(minutes=56),
            can_send=False,
        )
        self._add_chat(
            1006,
            "阿澈",
            "private",
            [
                ("阿澈", "上次你推荐的那本书我看完了。"),
                ("林间", "怎么样，是不是后半本更有意思？"),
                ("阿澈", "是的！边读边记了好多笔记。"),
                ("阿澈", "周六去旧书店逛逛？"),
            ],
            base - timedelta(hours=2),
        )
        self._add_chat(
            1007,
            "Open Source Café",
            "group",
            [
                ("Sam", "Pushed a small documentation improvement today."),
                ("Noah", "Those make a bigger difference than people think."),
                ("林间", "第一次贡献也是从改文档开始的。"),
                ("Sam", "Every little contribution counts. Thanks, everyone."),
            ],
            base - timedelta(hours=3),
        )
        self._add_chat(
            1008,
            "Slow Sunday",
            "channel",
            [
                ("Slow Sunday", "A reminder to leave a little room in your day."),
                ("Slow Sunday", "Walk without headphones. Notice one new thing."),
                ("Slow Sunday", "Less hurry. More here."),
            ],
            base - timedelta(hours=5),
            can_send=False,
        )
        self._add_chat(
            1009,
            "Noah",
            "private",
            [
                ("Noah", "Found a great walking trail near the river."),
                ("林间", "发给我，周末想出去走走。"),
                ("Noah", "I'll send you the route later. The sunset is worth it."),
            ],
            base - timedelta(days=1),
        )

    def _add_chat(
        self,
        chat_id: int,
        name: str,
        kind: str,
        lines: list[tuple[str, str]],
        latest: datetime,
        *,
        unread: int = 0,
        can_send: bool = True,
    ) -> None:
        self._dialogs[chat_id] = Dialog(
            id=chat_id,
            name=name,
            preview=lines[-1][1],
            unread=unread,
            date=latest,
            kind=kind,
            can_send=can_send,
        )
        self._messages[chat_id] = [
            ChatMessage(
                id=index + 1,
                chat_id=chat_id,
                sender=sender,
                text=text,
                date=latest - timedelta(minutes=(len(lines) - index - 1) * 3),
                outgoing=sender == self._me.name,
                chat_title=name,
            )
            for index, (sender, text) in enumerate(lines)
        ]
