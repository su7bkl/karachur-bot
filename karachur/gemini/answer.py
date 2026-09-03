"""
Ответ на реплику в чате: от строк истории до готового текста от модели.

Сам порядок шагов и есть содержание модуля. Ужать контекст до лимита, собрать из него
запрос, сходить в API с повторами, снять с ответа служебные пометки - каждый шаг умеет
делать кто-то из соседей по пакету, а здесь они складываются в одну дорогу, и наружу
торчит ровно одна функция. Telegram про эту кухню ничего не знает: karachur.tg.handlers
зовет generate_gemini_response и получает строку.

Запрос собирается не заранее, а корутиной под каждый ключ отдельно: ссылки на выгруженные
файлы принадлежат тому ключу, которым их выгружали, поэтому после ротации содержимое
запроса приходится строить заново - и решает это цикл повторов, а не мы.
"""

import asyncio
import logging
import sqlite3

from karachur import config
from karachur.gemini import compress

# Модуль зовется request_contents, а не contents: имя contents тут занято самими
# собираемыми списками содержимого запроса, и модуль ими бы перекрывался.
from karachur.gemini import contents as request_contents

# Ровно та же история с пулом: имя pool по всему коду занято самим пулом чата
# (аргументы обработчиков, поле сессии), и модуль под тем же именем ими бы перекрывался.
from karachur.gemini import pool as key_pool
from karachur.gemini import retries
from karachur.text import notes

logger = logging.getLogger(__name__)


async def generate_gemini_response(
    cfg: config.Config,
    pool: key_pool.KeyPool,
    conn: sqlite3.Connection,
    chat_id: int,
    context_messages: list,
    summary: str | None,
):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """
    Генерирует ответ с использованием модели Google Gemini AI на основе контекста чата.

    :param cfg: настройки бота
    :type cfg: config.Config
    :param pool: пул ключей этого чата, он же задает модель
    :type pool: key_pool.KeyPool
    :param conn: соединение с БД - нужно, чтобы сохранить пересказ
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param context_messages: список сообщений контекста
    :type context_messages: list
    :param summary: пересказ сжатой ранее части истории
    :type summary: str | None
    :return: сгенерированный ответ
    :rtype: str
    """
    logger.info(
        "Чат %s: подготовка %d сообщений контекста для модели %s.",
        chat_id,
        len(context_messages),
        pool.model,
    )
    if not context_messages:
        logger.warning("Контекст для Gemini пуст. Отмена запроса.")
        return "Не могу обработать пустой запрос."

    context_messages, summary = await compress.compress_context(
        cfg, pool, conn, chat_id, context_messages, summary
    )

    async def make_contents(key: dict) -> list:
        """Собирает запрос под конкретный ключ: медиа выгружается от его имени."""
        history = await asyncio.to_thread(
            request_contents.build_history, key, context_messages, cfg.media_dir
        )
        return request_contents.build_contents(history, summary, cfg.system_prompt)

    logger.info("Отправка запроса в Gemini...")

    # Генерируем ответ с новым API, повторяя попытки при сбоях
    response_text = await retries.generate_with_retries(cfg, pool, make_contents)
    return notes.strip_service_prefixes(response_text)
