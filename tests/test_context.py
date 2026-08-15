"""
Тесты истории: разделение по чатам, пересказы и схема базы.

Главное, что здесь проверяется, - что чаты не видят друг друга. В Telegram message_id
уникален только внутри чата, и любая выборка без chat_id рано или поздно смешивает
разговоры или затирает чужие сообщения.
"""

import sqlite3

import pytest

import bot
from conftest import CHAT_ONE, CHAT_TWO


def test_same_message_id_in_two_chats(db, add_message):
    """Одинаковые номера сообщений в разных чатах не затирают друг друга."""
    add_message(CHAT_ONE, 1, "привет из первого")
    add_message(CHAT_TWO, 1, "привет из второго")

    stored = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert stored == 2


def test_context_is_limited_to_its_chat(db, add_message):
    """В контекст чата попадают только его сообщения."""
    add_message(CHAT_ONE, 1, "первое сообщение чата А")
    add_message(CHAT_TWO, 1, "первое сообщение чата Б")
    add_message(CHAT_ONE, 2, "второе сообщение чата А")

    _, messages = bot.get_context(db, CHAT_ONE)

    assert [msg["content"] for msg in messages] == [
        "первое сообщение чата А",
        "второе сообщение чата А",
    ]


def test_reply_target_comes_from_the_same_chat(db, add_message):
    """Адресат ответа ищется в своем чате, а не по совпадению номера."""
    add_message(CHAT_ONE, 1, "вопрос чата А")
    add_message(CHAT_TWO, 1, "вопрос чата Б")
    add_message(CHAT_TWO, 5, "ответ чата Б", reply_to=1)

    _, messages = bot.get_context(db, CHAT_TWO)
    target = messages[-1]["reply_target"]

    assert target["content"] == "вопрос чата Б"


def test_missing_reply_target_is_described(db, add_message):
    """Ответ на сообщение старше бота помечается как отсутствующее в истории."""
    add_message(CHAT_ONE, 7, "ответ на что-то древнее", reply_to=1)

    _, messages = bot.get_context(db, CHAT_ONE)
    note = bot.build_service_note(messages[-1]) or ""

    assert "которого нет в истории" in note


def test_summary_belongs_to_one_chat(db, add_message):
    """Пересказ виден своему чату и не задевает соседний."""
    add_message(CHAT_ONE, 1, "старое сообщение чата А")
    add_message(CHAT_ONE, 2, "свежее сообщение чата А")
    add_message(CHAT_TWO, 1, "сообщение чата Б")

    bot.save_summary(db, CHAT_ONE, "пересказ чата А", [1])

    summary_one, messages_one = bot.get_context(db, CHAT_ONE)
    summary_two, messages_two = bot.get_context(db, CHAT_TWO)

    assert summary_one == "пересказ чата А"
    assert [msg["content"] for msg in messages_one] == ["свежее сообщение чата А"]
    assert summary_two is None
    assert len(messages_two) == 1


def test_summary_marks_only_its_own_messages(db, add_message):
    """Сообщение соседнего чата с тем же номером не помечается сжатым."""
    add_message(CHAT_ONE, 1, "сообщение чата А")
    add_message(CHAT_TWO, 1, "сообщение чата Б")

    bot.save_summary(db, CHAT_ONE, "пересказ чата А", [1])

    marked = db.execute(
        "SELECT chat_id FROM messages WHERE summarized = 1"
    ).fetchall()
    assert marked == [(CHAT_ONE,)]


def test_latest_summary_wins(db):
    """Из нескольких пересказов чата берется самый свежий."""
    bot.save_summary(db, CHAT_ONE, "первый пересказ", [])
    bot.save_summary(db, CHAT_ONE, "второй пересказ", [])

    assert bot.get_latest_summary(db, CHAT_ONE) == "второй пересказ"


def test_legacy_database_is_rejected(tmp_path, monkeypatch):
    """База прошлой версии не открывается: в ней истории чатов вперемешку."""
    legacy_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(legacy_path)
    legacy.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE)"
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(bot, "DB_FILE", str(legacy_path))
    monkeypatch.setattr(bot, "MEDIA_DIR", str(tmp_path / "media"))

    with pytest.raises(RuntimeError, match="Удалите или переименуйте"):
        bot.init_db()


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("animated_sticker", "анимированный стикер"),
        ("video_sticker", "видео-стикер"),
        ("animation", "гифка"),
        ("video_note", "видеосообщение кружком"),
        ("voice", "голосовое сообщение"),
        ("photo", "фото"),
        ("document", "файл"),
    ],
)
def test_attachment_kind_reaches_the_model(db, kind, expected):
    """Модель узнает, чем было вложение: после перекодирования это уже не видно."""
    db.execute(
        """
        INSERT INTO messages (message_id, chat_id, username, content, media_type,
                              file_id, mime_type, timestamp, is_bot, summarized)
        VALUES (1, ?, 'tester', '', ?, 'FILE', 'video/mp4',
                '2026-08-15T10:00:00', 0, 0)
        """,
        (CHAT_ONE, kind),
    )
    db.commit()

    _, messages = bot.get_context(db, CHAT_ONE)
    note = bot.build_service_note(messages[0]) or ""

    assert f"Вложение: {expected}" in note


def test_message_without_attachment_has_no_note(db, add_message):
    """Обычной реплике без вложения помечать нечего."""
    add_message(CHAT_ONE, 1, "просто текст")

    _, messages = bot.get_context(db, CHAT_ONE)

    assert bot.build_service_note(messages[0]) is None


def test_copied_attachment_note_is_stripped():
    """Если модель скопирует пометку о вложении, она срезается."""
    assert bot.strip_service_prefixes("[Вложение: гифка] ответ") == "ответ"
