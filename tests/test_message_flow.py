"""Входящие вложения: порядок подготовки, ошибки Telegram и разделение по чатам."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Message
from telegram.error import BadRequest, TimedOut

from conftest import CHAT_ONE, CHAT_TWO, KEY_ONE
from karachur.gemini import contents
from karachur.media import paths
from karachur.storage import messages
from karachur.tg import handlers


def incoming(chat_id, message_id, **fields):
    """Настоящий Message с минимальными полями Telegram."""
    message = Message.de_json({
        "message_id": message_id,
        "date": 1_700_000_000 + message_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": 5, "is_bot": False, "first_name": "Tester"},
        **fields,
    }, bot=None)
    return SimpleNamespace(effective_message=message)


PHOTO = [{"file_id": "PHOTO", "file_unique_id": "unique", "width": 8, "height": 8}]


@pytest.fixture(name="flow")
def flow_fixture(cfg, db, gemini, monkeypatch):  # pylint: disable=unused-argument
    """Доходим до сборки contents, подменяя отправку ответа в Telegram и Gemini."""
    seen = []

    async def generate(_cfg, _pool, _conn, chat_id, context_messages, _summary, **_kwargs):
        history = contents.build_history({"api_key": KEY_ONE}, context_messages, cfg.media_dir)
        seen.append((chat_id, history))
        return "готовый ответ"

    monkeypatch.setattr(handlers.answer, "generate_gemini_response", generate)
    monkeypatch.setattr(handlers.delivery, "send_placeholder", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers.delivery, "deliver_response", AsyncMock())
    context = SimpleNamespace(
        bot_data={"cfg": cfg, "db_conn": db},
        application=SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock())),
    )
    return context, seen


@pytest.mark.parametrize("pending_phase", ["download", "normalize"])
def test_question_waits_for_previous_attachment(db, flow, monkeypatch, pending_phase):
    """Вопрос ждет фото и его конвертацию; запись текста и другой чат продолжают работать."""
    context, seen = flow

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def download(_application, _file_id, path):
            if pending_phase == "download":
                started.set()
                await release.wait()
            Path(path).write_bytes(b"original")

        async def normalize(conn, message, path, _mime):
            if pending_phase == "normalize":
                started.set()
                await release.wait()
            converted = Path(path).with_suffix(".png")
            converted.write_bytes(b"converted")
            messages.set_media_path(
                conn, message.chat_id, message.message_id, str(converted), "image/png"
            )

        monkeypatch.setattr(paths, "download_media_file", download)
        monkeypatch.setattr(handlers, "normalize_media", normalize)
        attachment = asyncio.create_task(
            handlers.handle_message(incoming(CHAT_ONE, 1, photo=PHOTO), context)
        )
        await asyncio.wait_for(started.wait(), 2)
        question = asyncio.create_task(handlers.handle_message(
            incoming(CHAT_ONE, 2, text="Карачур, что на картинке?"), context
        ))
        await asyncio.sleep(0)
        assert not seen
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2

        # Обычный текст не ждет замка, а другой чат может получить ответ сразу.
        await handlers.handle_message(incoming(CHAT_ONE, 3, text="обычная реплика"), context)
        await handlers.handle_message(incoming(CHAT_TWO, 1, text="Карачур, привет"), context)
        assert [chat_id for chat_id, _ in seen] == [CHAT_TWO]
        release.set()
        await asyncio.wait_for(asyncio.gather(attachment, question), 2)

    asyncio.run(scenario())
    chat_id, history = seen[-1]
    assert chat_id == CHAT_ONE
    assert [entry["source"]["message_id"] for entry in history] == [1, 2]
    assert history[0]["source"]["mime_type"] == "image/png"
    assert any(part.file_data for part in history[0]["parts"])


@pytest.mark.parametrize("failed_call", ["get_file", "download_to_drive"])
@pytest.mark.parametrize("error", [TimedOut("timeout"), BadRequest("file unavailable")])
def test_failed_download_keeps_answer_and_does_not_use_legacy_file(
    cfg, db, flow, gemini, failed_call, error
):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Отказ Telegram не обрывает ответ и не подставляет старый общий файл."""
    context, seen = flow
    legacy = Path(paths.get_media_path(cfg.media_dir, "PHOTO", "image/jpeg", None))
    legacy.write_bytes(b"old shared file")
    remote = SimpleNamespace(download_to_drive=AsyncMock())
    context.application.bot.get_file.return_value = remote
    if failed_call == "get_file":
        context.application.bot.get_file.side_effect = error
    else:
        remote.download_to_drive.side_effect = error

    async def scenario():
        await handlers.handle_message(
            incoming(CHAT_ONE, 1, photo=PHOTO, caption="Карачур, посмотри"), context
        )
        await handlers.handle_message(
            incoming(CHAT_ONE, 2, text="Карачур, следующий вопрос"), context
        )

    asyncio.run(scenario())
    assert len(seen) == 2
    parts = seen[0][1][0]["parts"]
    assert any("Вложение недоступно" in (part.text or "") for part in parts)
    assert not any(part.file_data for part in parts)
    assert gemini.file_uploads == []
    stored = db.execute("SELECT media_path FROM messages WHERE message_id = 1").fetchone()[0]
    assert str(CHAT_ONE) in Path(stored).name
    assert legacy.read_bytes() == b"old shared file"


def test_new_downloads_have_separate_paths_and_leave_old_file_intact(cfg, db, flow):
    """Один file_id в двух чатах сохраняется отдельно; старый файл остается на месте."""
    context, _ = flow
    legacy = Path(paths.get_media_path(cfg.media_dir, "PHOTO", "image/jpeg", None))
    legacy.write_bytes(b"legacy")

    async def download(path):
        Path(path).write_bytes(b"new")

    context.application.bot.get_file.return_value = SimpleNamespace(download_to_drive=download)

    async def scenario():
        await asyncio.gather(*(
            handlers.handle_message(incoming(chat_id, 1, photo=PHOTO), context)
            for chat_id in (CHAT_ONE, CHAT_TWO)
        ))

    asyncio.run(scenario())
    rows = db.execute("SELECT chat_id, media_path FROM messages").fetchall()
    assert len({row["media_path"] for row in rows}) == 2
    for row in rows:
        path = Path(row["media_path"])
        assert str(row["chat_id"]) in path.name
        assert path.read_bytes() == b"new"
    assert legacy.read_bytes() == b"legacy"
    assert context.application.bot.get_file.await_count == 2
