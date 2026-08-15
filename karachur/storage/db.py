"""
Открытие соединения с SQLite и разбор строк результата.

Модуль не знает про таблицы бота - это чистая обвязка над sqlite3, на которой строится
karachur.storage.schema и весь остальной код, читающий базу. Вынесена отдельно от схемы,
потому что открытие соединения и разбор строк нужны и там, где о самих таблицах речи не
идет (например, в api_keys, который работает со своими таблицами через это же соединение).
"""

import sqlite3


def connect(db_file: str) -> sqlite3.Connection:
    """
    Открывает соединение с базой и настраивает выборку строк по имени колонки.

    :param db_file: путь к файлу базы
    :type db_file: str
    :return: открытое соединение
    :rtype: sqlite3.Connection
    """
    conn = sqlite3.connect(db_file, check_same_thread=False)
    # sqlite3.Row ведет себя как обычный кортеж - row[0] и распаковка a, b = row
    # по-прежнему работают, - но вдобавок понимает доступ по имени колонки и dict(row).
    # Без этого код трижды в разных модулях вручную собирал dict(zip(columns, row))
    # через cursor.description.
    conn.row_factory = sqlite3.Row
    return conn


def fetch_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """
    Превращает результат выполненного запроса в список обычных словарей.

    :param cursor: курсор с выполненным запросом
    :type cursor: sqlite3.Cursor
    :return: строки результата
    :rtype: list[dict]
    """
    return [dict(row) for row in cursor.fetchall()]
