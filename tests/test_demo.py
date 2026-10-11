import asyncio

import pytest

from telegram_tui.demo import DemoBackend


@pytest.fixture
async def backend():
    instance = DemoBackend(reply_delay=60)
    assert await instance.connect()
    yield instance
    await instance.disconnect()


async def test_fixtures_are_deterministic_and_start_with_terminal_club(backend):
    dialogs = await backend.get_dialogs()
    assert len(dialogs) == 9
    assert dialogs == await DemoBackend().get_dialogs()
    assert dialogs[0].name == "Terminal Club"
    assert {dialog.kind for dialog in dialogs} == {"saved", "group", "channel", "private"}
    assert all(not dialog.can_send for dialog in dialogs if dialog.kind == "channel")
    me = await backend.get_me()
    assert (me.name, me.username) == ("林间", "terminal_user")


async def test_paginate_history_without_duplicates_or_wrong_order(backend):
    newest = await backend.get_messages(1001)
    older = await backend.get_messages(1001, before_id=newest[0].id)
    assert len(newest) == 50
    assert len(older) > 0
    combined = older + newest
    assert combined == await backend.get_messages(1001, limit=200)
    assert len({message.id for message in combined}) == len(combined)
    assert len({message.text for message in combined}) == len(combined)
    assert [message.date for message in combined] == sorted(message.date for message in combined)
    assert await backend.get_messages(1001, before_id=combined[0].id) == []
    assert await backend.get_messages(1001, limit=0) == []


async def test_send_emits_once_and_updates_history_and_preview(backend):
    events = []

    async def handler(message):
        events.append(message)

    backend.set_message_handler(handler)
    previous = (await backend.get_messages(1002))[-1]
    sent = await backend.send_message(1002, "  hello from the terminal  ")
    assert sent.id == previous.id + 1
    assert sent.outgoing
    assert sent.text == "  hello from the terminal  "
    assert sent.sender == "林间"
    assert events == [sent]
    assert (await backend.get_messages(1002))[-1] == sent
    updated = (await backend.get_dialogs())[0]
    assert (updated.id, updated.preview, updated.date) == (1002, sent.text, sent.date)


async def test_incoming_increments_unread_and_mark_read_clears_it(backend):
    events = []

    async def handler(message):
        events.append(message)

    backend.set_message_handler(handler)
    await backend.mark_read(1001)
    incoming = await backend.inject_message(1001, "新消息", sender="访客")
    assert not incoming.outgoing
    assert incoming.sender == "访客"
    assert events == [incoming]
    assert (await backend.get_dialogs())[0].unread == 1
    await backend.mark_read(1001)
    assert (await backend.get_dialogs())[0].unread == 0


async def test_external_mutation_cannot_change_backend_state(backend):
    dialogs = await backend.get_dialogs()
    original_name = dialogs[0].name
    dialogs[0].name = "changed"
    assert (await backend.get_dialogs())[0].name == original_name
    messages = await backend.get_messages(1001)
    original_text = messages[-1].text
    messages[-1].text = "changed"
    assert (await backend.get_messages(1001))[-1].text == original_text
    me = await backend.get_me()
    me.name = "changed"
    assert (await backend.get_me()).name == "林间"

    async def mutating_handler(message):
        message.text = "modified event"

    backend.set_message_handler(mutating_handler)
    returned = await backend.send_message(1003, "original")
    assert returned.text == "original"
    returned.text = "modified return"
    assert (await backend.get_messages(1003))[-1].text == "original"


async def test_delayed_reply_is_incoming_and_contextual():
    backend = DemoBackend(reply_delay=0)
    await backend.connect()
    received = asyncio.Event()
    events = []

    async def handler(message):
        events.append(message)
        if not message.outgoing:
            received.set()

    backend.set_message_handler(handler)
    try:
        sent = await backend.send_message(1001, "这个主题不错")
        await asyncio.wait_for(received.wait(), timeout=1)
        assert len(events) == 2
        assert events[0] == sent
        assert not events[1].outgoing
        assert events[1].id == sent.id + 1
        assert "配色" in events[1].text
    finally:
        await backend.disconnect()


async def test_disconnect_cancels_pending_replies_and_can_reconnect(backend):
    await backend.send_message(1001, "hello")
    tasks = tuple(backend._tasks)
    assert tasks
    before = await backend.get_messages(1001)
    await backend.disconnect()
    assert all(task.done() for task in tasks)
    assert not backend._tasks
    assert await backend.get_messages(1001) == before
    with pytest.raises(ConnectionError):
        await backend.send_message(1001, "after disconnect")
    with pytest.raises(ConnectionError):
        await backend.inject_message(1001, "after disconnect")
    assert await backend.connect()
    await backend.send_message(1003, "saved after reconnect")


async def test_saved_messages_do_not_schedule_a_reply(backend):
    await backend.send_message(1003, "A private note")
    assert not backend._tasks


async def test_invalid_chat_empty_message_and_read_only_channel_are_rejected(backend):
    dialogs_before = await backend.get_dialogs()
    for action in (
        backend.get_messages(-123),
        backend.send_message(-123, "hello"),
        backend.inject_message(-123, "hello"),
        backend.mark_read(-123),
        backend.send_message(1001, "  \n "),
    ):
        with pytest.raises(ValueError):
            await action
    with pytest.raises(PermissionError):
        await backend.send_message(1005, "channel message")
    assert await backend.get_dialogs() == dialogs_before
    assert not backend._tasks
