"""
Тесты истории: разделение по чатам, пересказы и схема базы.

Главное, что здесь проверяется, - что чаты не видят друг друга. В Telegram message_id
уникален только внутри чата, и любая выборка без chat_id рано или поздно смешивает
разговоры или затирает чужие сообщения.
"""

import datetime
import sqlite3
import types

import pytest

from conftest import CHAT_ONE, CHAT_TWO
from karachur.storage import messages, schema, summaries
from karachur.text import notes


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

    _, context = messages.get_context(db, CHAT_ONE)

    assert [msg["content"] for msg in context] == [
        "первое сообщение чата А",
        "второе сообщение чата А",
    ]


def test_reply_target_comes_from_the_same_chat(db, add_message):
    """Адресат ответа ищется в своем чате, а не по совпадению номера."""
    add_message(CHAT_ONE, 1, "вопрос чата А")
    add_message(CHAT_TWO, 1, "вопрос чата Б")
    add_message(CHAT_TWO, 5, "ответ чата Б", reply_to=1)

    _, context = messages.get_context(db, CHAT_TWO)
    target = context[-1]["reply_target"]

    assert target["content"] == "вопрос чата Б"


def test_missing_reply_target_is_described(db, add_message):
    """Ответ на сообщение старше бота помечается как отсутствующее в истории."""
    add_message(CHAT_ONE, 7, "ответ на что-то древнее", reply_to=1)

    _, context = messages.get_context(db, CHAT_ONE)
    note = notes.build_service_note(context[-1]) or ""

    assert "которого нет в истории" in note


def test_summary_belongs_to_one_chat(db, add_message):
    """Пересказ виден своему чату и не задевает соседний."""
    add_message(CHAT_ONE, 1, "старое сообщение чата А")
    add_message(CHAT_ONE, 2, "свежее сообщение чата А")
    add_message(CHAT_TWO, 1, "сообщение чата Б")

    summaries.save_summary(db, CHAT_ONE, "пересказ чата А", [1])

    summary_one, messages_one = messages.get_context(db, CHAT_ONE)
    summary_two, messages_two = messages.get_context(db, CHAT_TWO)

    assert summary_one == "пересказ чата А"
    assert [msg["content"] for msg in messages_one] == ["свежее сообщение чата А"]
    assert summary_two is None
    assert len(messages_two) == 1


def test_summary_marks_only_its_own_messages(db, add_message):
    """Сообщение соседнего чата с тем же номером не помечается сжатым."""
    add_message(CHAT_ONE, 1, "сообщение чата А")
    add_message(CHAT_TWO, 1, "сообщение чата Б")

    summaries.save_summary(db, CHAT_ONE, "пересказ чата А", [1])

    marked = db.execute(
        "SELECT chat_id FROM messages WHERE summarized = 1"
    ).fetchall()
    # row_factory отдает sqlite3.Row, а не кортеж - он с кортежем не равен даже при
    # совпадении значений, поэтому сравниваем приведенные к tuple() строки.
    assert [tuple(row) for row in marked] == [(CHAT_ONE,)]


def test_latest_summary_wins(db):
    """Из нескольких пересказов чата берется самый свежий."""
    summaries.save_summary(db, CHAT_ONE, "первый пересказ", [])
    summaries.save_summary(db, CHAT_ONE, "второй пересказ", [])

    assert summaries.get_latest_summary(db, CHAT_ONE) == "второй пересказ"


def test_legacy_database_is_rejected(tmp_path):
    """База прошлой версии не открывается: в ней истории чатов вперемешку."""
    legacy_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(legacy_path)
    legacy.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE)"
    )
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="Удалите или переименуйте"):
        schema.init_db(str(legacy_path), str(tmp_path / "media"))


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

    _, context = messages.get_context(db, CHAT_ONE)
    note = notes.build_service_note(context[0]) or ""

    assert f"Вложение: {expected}" in note


def test_message_without_attachment_has_no_note(db, add_message):
    """Обычной реплике без вложения помечать нечего."""
    add_message(CHAT_ONE, 1, "просто текст")

    _, context = messages.get_context(db, CHAT_ONE)

    assert notes.build_service_note(context[0]) is None


def test_copied_attachment_note_is_stripped():
    """Если модель скопирует пометку о вложении, она срезается."""
    assert notes.strip_service_prefixes("[Вложение: гифка] ответ") == "ответ"


def test_context_stops_at_the_asking_message(db, add_message):
    """
    Запрос видит историю по свое сообщение, а не все, что накопилось потом.

    Сообщения кладутся в базу сразу, не дожидаясь очереди на ответ, а ответ с повторами
    тянется минутами. Без границы бот отвечал бы на вопрос, подглядывая в написанное
    уже после этого вопроса.
    """
    add_message(CHAT_ONE, 1, "что было раньше")
    add_message(CHAT_ONE, 2, "Карачур, вопрос")
    add_message(CHAT_ONE, 3, "написали, пока бот думал")

    _, context = messages.get_context(db, CHAT_ONE, up_to_message_id=2)

    assert [msg["content"] for msg in context] == ["что было раньше", "Карачур, вопрос"]


def test_bound_keeps_neighbours_of_the_same_second(db, add_message):
    """
    Сообщения с той же меткой времени отсекаются по номеру, а не заодно с ней.

    Время у Telegram с точностью до секунды, поэтому соседи по секунде - обычное дело, и
    граница обязана различать их. Оснастка выводит метку из номера, так что 5 и 65
    делят одну секунду.
    """
    add_message(CHAT_ONE, 5, "спросили")
    add_message(CHAT_ONE, 65, "в ту же секунду написали еще")

    _, context = messages.get_context(db, CHAT_ONE, up_to_message_id=5)

    assert [msg["content"] for msg in context] == ["спросили"]


def test_unknown_bound_falls_back_to_the_whole_history(db, add_message):
    """
    Сообщения-границы нет в базе - отдаем всю историю, а не пустоту.

    Пустой контекст означал бы отказ отвечать, а это хуже лишней реплики в запросе.
    """
    add_message(CHAT_ONE, 1, "первое")
    add_message(CHAT_ONE, 2, "второе")

    _, context = messages.get_context(db, CHAT_ONE, up_to_message_id=999)

    assert [msg["content"] for msg in context] == ["первое", "второе"]


class _TelegramMessage:
    """
    Сообщение Telegram в объеме, который читает save_message_to_db.

    Настоящий telegram.Message для этого не собрать: у него нужны и Bot, и полный набор
    полей вложений. Здесь хватает того, что обработчик действительно спрашивает, - и
    edit_date, по которому видно, что это правка уже отправленного сообщения.
    """

    # Поля вложений перечислены пустыми: extract_attachment перебирает их все подряд.
    photo = sticker = animation = video = video_note = voice = audio = document = None
    caption = None
    reply_to_message = None
    quote = None
    forward_origin = None

    def __init__(self, chat_id: int, message_id: int, text: str, edited: bool = False):
        """
        :param chat_id: идентификатор чата
        :type chat_id: int
        :param message_id: номер сообщения в чате
        :type message_id: int
        :param text: текст реплики
        :type text: str
        :param edited: это правка ранее отправленного сообщения
        :type edited: bool
        """
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.date = datetime.datetime(2026, 8, 15, 10, 0, 0)
        self.edit_date = (
            datetime.datetime(2026, 8, 15, 10, 5, 0) if edited else None
        )
        self.from_user = types.SimpleNamespace(
            id=1, full_name="tester", username="tester"
        )


def test_edit_does_not_undo_compression(db):
    """
    Правка сжатого сообщения не возвращает его в контекст.

    Пометка summarized - бухгалтерия сжатия, а не свойство самого сообщения: сброси ее,
    и реплика поедет в запрос дословно, оставаясь при этом в пересказе. Правка одной
    старой реплики так молча раскручивала сжатие назад.
    """
    messages.save_message_to_db(db, _TelegramMessage(CHAT_ONE, 1, "старое"))
    messages.save_message_to_db(db, _TelegramMessage(CHAT_ONE, 2, "свежее"))
    summaries.save_summary(db, CHAT_ONE, "пересказ старого", [1])

    messages.save_message_to_db(
        db, _TelegramMessage(CHAT_ONE, 1, "старое (исправлено)", edited=True)
    )

    summary, context = messages.get_context(db, CHAT_ONE)

    assert summary == "пересказ старого"
    assert [msg["content"] for msg in context] == ["свежее"]


def test_edit_updates_the_text(db):
    """Правка все-таки доезжает до базы - обновляется то, что и должно."""
    messages.save_message_to_db(db, _TelegramMessage(CHAT_ONE, 1, "как было"))
    messages.save_message_to_db(
        db, _TelegramMessage(CHAT_ONE, 1, "как стало", edited=True)
    )

    _, context = messages.get_context(db, CHAT_ONE)

    assert [msg["content"] for msg in context] == ["как стало"]


def test_edit_does_not_duplicate_the_message(db):
    """Повторное сохранение той же пары (чат, номер) не плодит строк."""
    messages.save_message_to_db(db, _TelegramMessage(CHAT_ONE, 1, "как было"))
    messages.save_message_to_db(
        db, _TelegramMessage(CHAT_ONE, 1, "как стало", edited=True)
    )

    stored = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert stored == 1
