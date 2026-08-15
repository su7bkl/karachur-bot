"""
Схема базы данных бота: создание таблиц с нуля и донастройка баз прошлых версий.

Раньше это жило в bot.py вместе со всем остальным. Здесь именно форма таблиц и
индексов - работа с данными в них (чтение и запись сообщений, пересказов, настроек
чата, ключей) остается в своих модулях: bot.py, karachur.storage.settings, api_keys.
"""

import logging
import os
import sqlite3

import api_keys
from karachur.storage import db, settings

logger = logging.getLogger(__name__)


def init_db(db_file: str, media_dir: str) -> sqlite3.Connection:
    """
    Инициализирует базу данных SQLite и создает необходимые таблицы.

    :param db_file: путь к файлу базы
    :type db_file: str
    :param media_dir: каталог для скачанных медиафайлов, заводится заодно с базой
    :type media_dir: str
    :return: соединение с базой данных
    :rtype: sqlite3.Connection
    """
    os.makedirs(media_dir, exist_ok=True)
    conn = db.connect(db_file)
    cursor = conn.cursor()
    check_legacy_schema(cursor, db_file)
    # message_id уникален только внутри чата: в двух разных чатах номера повторяются, и
    # без chat_id в ограничении сообщения одного чата затирали бы сообщения другого.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            content TEXT,
            media_type TEXT,
            mime_type TEXT,
            file_id TEXT,
            file_name TEXT,
            timestamp TEXT,
            reply_to_message_id INTEGER,
            quote_text TEXT,
            forward_origin TEXT,
            is_bot BOOLEAN DEFAULT 0,
            summarized INTEGER DEFAULT 0,
            media_path TEXT,
            UNIQUE (chat_id, message_id)
        )
    """)
    add_missing_columns(cursor)
    # Контекст собирается по одному чату, и почти всегда - по его несжатой части.
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS messages_chat_context
        ON messages (chat_id, summarized, timestamp)
    """)
    # Пересказы сжатых кусков истории. Сами сообщения остаются в messages, но в контекст
    # больше не попадают: их заменяет последний пересказ своего чата.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS context_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            summary TEXT,
            covered_messages INTEGER,
            created_at TEXT
        )
    """)
    api_keys.init_key_tables(cursor)
    settings.init_settings_table(cursor)
    conn.commit()
    return conn


# Колонки messages, появившиеся после того, как схема уже уехала на боевую машину.
LATE_MESSAGE_COLUMNS = {"media_path": "TEXT"}


def add_missing_columns(cursor: sqlite3.Cursor):
    """
    Дописывает в messages колонки, которых нет в базах прошлых версий.

    :param cursor: курсор открытой базы
    :type cursor: sqlite3.Cursor
    """
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(messages)")}
    for name, definition in LATE_MESSAGE_COLUMNS.items():
        if name not in columns:
            cursor.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
            logger.info("В таблицу messages добавлена колонка %s.", name)


def check_legacy_schema(cursor: sqlite3.Cursor, db_file: str):
    """
    Отказывается работать с базой, созданной до разделения истории по чатам.

    В старой схеме message_id уникален сам по себе, а пересказы не привязаны к чату:
    подпереть это ALTER TABLE нельзя, а молча продолжить - значит перемешать истории
    разных чатов. Поэтому просто говорим, что делать.

    :param cursor: курсор открытой базы
    :type cursor: sqlite3.Cursor
    :param db_file: путь к файлу базы - его называем человеку в тексте отказа
    :type db_file: str
    :raises RuntimeError: если база сделана прошлой версией бота
    """
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
    ).fetchone()
    if not row or not row[0]:
        return

    schema = " ".join(row[0].split()).lower()
    if "unique (chat_id, message_id)" in schema:
        return

    raise RuntimeError(
        f"База {db_file} сделана версией бота без поддержки нескольких чатов: "
        "в ней истории всех чатов лежат вперемешку, а message_id уникален глобально. "
        "Удалите или переименуйте файл базы - новая создастся сама."
    )
