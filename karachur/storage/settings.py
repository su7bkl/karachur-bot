"""
Настройки отдельного чата: модель Gemini и указатель на активный ключ.

Бот обслуживает несколько чатов сразу, и у каждого своя жизнь: своя история, свой пул
ключей, своя модель. То, что чат меняет командами, лежит здесь; то, что задается один
раз на весь запуск, - в config.cfg.
"""

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def init_settings_table(cursor: sqlite3.Cursor):
    """
    Создает таблицу настроек чатов, если ее еще нет.

    :param cursor: курсор открытой базы
    :type cursor: sqlite3.Cursor
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            model TEXT,
            active_key_id INTEGER,
            updated_at TEXT
        )
    """)


def _set_field(conn: sqlite3.Connection, chat_id: int, column: str, value):
    """
    Записывает одно поле настроек, заводя строку чата при необходимости.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param column: имя колонки
    :type column: str
    :param value: новое значение
    """
    # Имя колонки приходит только из кода этого модуля, снаружи в запрос ничего не попадает.
    conn.execute(
        f"""
        INSERT INTO chat_settings (chat_id, {column}, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE
            SET {column} = excluded.{column}, updated_at = excluded.updated_at
        """,
        (chat_id, value, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _get_field(conn: sqlite3.Connection, chat_id: int, column: str):
    """
    Читает одно поле настроек чата.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param column: имя колонки
    :type column: str
    :return: значение поля или None, если чат еще ничего не настраивал
    """
    # Имя колонки приходит только из кода этого модуля, снаружи в запрос ничего не попадает.
    cursor = conn.execute(
        f"SELECT {column} FROM chat_settings WHERE chat_id = ?", (chat_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_model(conn: sqlite3.Connection, chat_id: int, default: str) -> str:
    """
    Возвращает модель, выбранную чатом, или общую по умолчанию.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param default: модель из config.cfg
    :type default: str
    :return: имя модели Gemini
    :rtype: str
    """
    return _get_field(conn, chat_id, "model") or default


def set_model(conn: sqlite3.Connection, chat_id: int, model: str):
    """
    Запоминает модель, выбранную чатом.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param model: имя модели Gemini
    :type model: str
    """
    _set_field(conn, chat_id, "model", model)
    logger.info("Чат %s переключен на модель %s.", chat_id, model)


def reset_model(conn: sqlite3.Connection, chat_id: int):
    """
    Возвращает чат на модель по умолчанию из config.cfg.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    """
    _set_field(conn, chat_id, "model", None)
    logger.info("Чат %s вернулся на модель по умолчанию.", chat_id)


def get_active_key_id(conn: sqlite3.Connection, chat_id: int) -> int | None:
    """
    Возвращает идентификатор ключа, которым чат пользуется сейчас.

    Указатель живет в базе, а не в памяти: после перезапуска бот продолжает с того же
    ключа, а не начинает заново жечь первый по списку.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :return: идентификатор ключа или None, если чат еще ни разу не ходил в API
    :rtype: int | None
    """
    return _get_field(conn, chat_id, "active_key_id")


def set_active_key_id(conn: sqlite3.Connection, chat_id: int, key_id: int):
    """
    Запоминает ключ, на который переключился чат.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param key_id: идентификатор ключа в таблице api_keys
    :type key_id: int
    """
    _set_field(conn, chat_id, "active_key_id", key_id)
