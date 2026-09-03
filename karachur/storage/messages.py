"""
История сообщений чата: запись реплик в базу и сборка контекста для модели.

Раньше это лежало в bot.py вперемешку с разговором с Gemini. Здесь - только работа с
таблицей messages: что из объекта Telegram вообще доходит до базы, как к реплике-ответу
подкладывается ее адресат и какой кусок истории уходит в запрос. Форма самой таблицы -
в karachur.storage.schema, пересказы сжатой части истории - в karachur.storage.summaries.

Соединение отдает строки как sqlite3.Row (см. karachur.storage.db), поэтому наружу
выборки уходят через db.fetch_dicts: дальше по коду сообщения правят на месте (дописывают
"reply_target", подменяют content), а Row - только для чтения.
"""

import logging
import sqlite3
from datetime import datetime

from telegram import Message

from karachur.storage import db, summaries
from karachur.text import notes
from karachur.tg import extract

logger = logging.getLogger(__name__)


def save_message_to_db(
    conn: sqlite3.Connection,
    message: Message,
    is_bot: bool = False,
    content_override: str | None = None,
):
    """
    Сохраняет сообщение в базу данных.

    Вложение здесь только описывается - что это и под каким file_id лежит в Telegram.
    Скачивание и перекодирование идут потом и дописывают в ту же строку путь к файлу
    (см. set_media_path).

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param message: объект сообщения Telegram
    :type message: Message
    :param is_bot: сообщение написано самим ботом
    :type is_bot: bool
    :param content_override: текст, который попадет в контекст вместо реального текста
        сообщения. Нужен, чтобы простыня с ошибкой API не засоряла историю
    :type content_override: str | None
    :return: (file_id, mime_type, file_name) - информация о медиафайле, если он есть
    :rtype: tuple
    """
    cursor = conn.cursor()
    content = message.text or message.caption or ""

    attachment = extract.extract_attachment(message)
    if attachment.content_override is not None:
        content = attachment.content_override

    if content_override is not None:
        content = content_override

    timestamp = datetime.fromtimestamp(message.date.timestamp()).isoformat()
    reply_to_id = (
        message.reply_to_message.message_id if message.reply_to_message else None
    )
    # Фрагмент, который отвечающий выделил в чужом сообщении. Есть и у цитат из других
    # чатов - там это единственный след того, чему отвечали: reply_to_message_id пуст.
    quote_text = message.quote.text if message.quote else None
    # Настоящий автор пересланного: в from_user ниже стоит тот, кто нажал "переслать".
    forward_origin = notes.describe_forward_origin(message.forward_origin)
    user_id = message.from_user.id if message.from_user else None
    if message.from_user:
        date = (
            str(message.date)
            if not message.edit_date
            else str(message.date) + "/edited:" + str(message.edit_date)
        )
        user_prompt = notes.build_author_tag(
            message.from_user.full_name or str(message.from_user.id),
            message.from_user.username,
            date,
        )
    else:
        user_prompt = "Bot"

    cursor.execute(
        """
        INSERT OR REPLACE INTO messages (
            message_id, chat_id, user_id, username, content, media_type,
            mime_type, file_id, file_name, timestamp, reply_to_message_id,
            quote_text, forward_origin, is_bot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            message.message_id,
            message.chat_id,
            user_id,
            user_prompt,
            content,
            attachment.media_type,
            attachment.mime_type,
            attachment.file_id,
            attachment.file_name,
            timestamp,
            reply_to_id,
            quote_text,
            forward_origin,
            is_bot,
        ),
    )
    conn.commit()
    logger.info("Сохранено сообщение %s в БД.", message.message_id)  # lazy logging
    return attachment.file_id, attachment.mime_type, attachment.file_name


def set_media_path(
    conn: sqlite3.Connection,
    chat_id: int,
    message_id: int,
    path: str,
    mime: str,
):
    """
    Запоминает, куда лег перекодированный файл сообщения и чем он стал.

    Отдельный шаг после save_message_to_db, потому что к моменту вставки строки файл еще
    не скачан. Путь приходится хранить: после ffmpeg у файла другое расширение, и вывести
    его из mime, как раньше, уже нельзя. Медиаконвейер сам SQL не пишет - вся работа с
    таблицей messages собрана здесь.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата - message_id уникален только внутри него
    :type chat_id: int
    :param message_id: номер сообщения, к которому относится файл
    :type message_id: int
    :param path: путь к файлу после перекодирования
    :type path: str
    :param mime: mime, с которым файл уйдет в модель
    :type mime: str
    """
    conn.execute(
        """
        UPDATE messages SET media_path = ?, mime_type = ?
        WHERE chat_id = ? AND message_id = ?
        """,
        (path, mime, chat_id, message_id),
    )
    conn.commit()


def attach_reply_targets(conn: sqlite3.Connection, chat_id: int, messages: list):
    """
    Подкладывает к каждой реплике-ответу сообщение, которому она отвечает.

    Ищем по всей истории чата, а не по переданному куску: адресат мог остаться далеко
    позади и уже уйти в пересказ, но пометка о нем все равно нужна. За пределы чата не
    выходим - там лежат чужие разговоры с такими же номерами сообщений.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param messages: сообщения контекста; в отвечающие добавляется ключ "reply_target"
        со строкой адресата или None, если такого сообщения в базе нет
    :type messages: list
    """
    target_ids = {
        msg["reply_to_message_id"] for msg in messages if msg.get("reply_to_message_id")
    }
    if not target_ids:
        return

    cursor = conn.cursor()
    # В строку запроса подставляем только число "?" - сами идентификаторы идут параметрами.
    cursor.execute(
        f"""
        SELECT * FROM messages
        WHERE chat_id = ? AND message_id IN ({",".join("?" * len(target_ids))})
        """,
        (chat_id, *target_ids),
    )
    targets = {target["message_id"]: target for target in db.fetch_dicts(cursor)}

    for msg in messages:
        reply_to_id = msg.get("reply_to_message_id")
        if reply_to_id:
            msg["reply_target"] = targets.get(reply_to_id)


def get_context(conn: sqlite3.Connection, chat_id: int) -> tuple[str | None, list]:
    """
    Получает контекст чата: пересказ старой части истории и сообщения после нее.

    Пока сжатия не было, пересказ пуст и возвращается вся история чата. Чужие чаты в
    контекст не попадают: у каждого своя история, свой пересказ и свои ключи.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :return: (текст пересказа или None, список словарей с информацией о сообщениях;
        у реплик-ответов в ключе "reply_target" лежит сообщение, которому они отвечают)
    :rtype: tuple[str | None, list]
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM messages WHERE chat_id = ? AND summarized = 0
        ORDER BY timestamp ASC, message_id ASC
    """,
        (chat_id,),
    )
    messages = db.fetch_dicts(cursor)
    attach_reply_targets(conn, chat_id, messages)
    return summaries.get_latest_summary(conn, chat_id), messages
