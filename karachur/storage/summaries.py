"""
Пересказы сжатой части истории чата.

Контекст модели не резиновый, поэтому старые сообщения уходят в один связный текст.
Сами реплики из базы никуда не деваются - они просто помечаются сжатыми и больше не
попадают в контекст: их место занимает последний пересказ своего чата.

Отделено от karachur.storage.messages, потому что это разные таблицы и разные жизненные
циклы: messages пишется на каждое сообщение чата, context_summaries - раз в много
сообщений, когда запрос перестает влезать в лимит.
"""

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def get_latest_summary(conn: sqlite3.Connection, chat_id: int) -> str | None:
    """
    Возвращает последний пересказ сжатой части истории этого чата.

    Каждый следующий пересказ вбирает в себя предыдущий, поэтому актуален всегда
    только самый свежий.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :return: текст пересказа или None, если сжатия еще не было
    :rtype: str | None
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT summary FROM context_summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def save_summary(
    conn: sqlite3.Connection, chat_id: int, summary: str, message_ids: list
):
    """
    Сохраняет пересказ и помечает вошедшие в него сообщения как сжатые.

    Помечаем поименно, а не по времени последнего сжатого сообщения: сдвиг часов или
    сообщение, пришедшее с запозданием, увели бы границу по времени не туда, и кусок
    истории молча выпал бы из контекста.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param summary: текст пересказа
    :type summary: str
    :param message_ids: message_id сообщений, вошедших в пересказ
    :type message_ids: list
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO context_summaries (chat_id, summary, covered_messages, created_at)
        VALUES (?, ?, ?, ?)
    """,
        (chat_id, summary, len(message_ids), datetime.now(timezone.utc).isoformat()),
    )
    # message_id уникален только внутри чата, поэтому помечаем строго свои сообщения.
    cursor.executemany(
        "UPDATE messages SET summarized = 1 WHERE chat_id = ? AND message_id = ?",
        [(chat_id, message_id) for message_id in message_ids],
    )
    conn.commit()
    logger.info("Чат %s: сохранен пересказ %d сообщений.", chat_id, len(message_ids))
