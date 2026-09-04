"""
Отправка готового ответа в чат: заглушка на время генерации и доставка итогового текста.

Gemini отвечает не мгновенно, а с повторами (см. karachur.gemini.retries) это может
растянуться на минуты. Молчание бота в этот момент читается человеком как зависание или
проигнорированное сообщение, поэтому сразу после запроса в чат уходит заглушка
GENERATING_PLACEHOLDER - она же потом редактируется в готовый ответ, а не удаляется и не
заменяется новым сообщением, чтобы у ответа в чате осталось то же место, что и у заглушки.

Пока ответ собирается, эту же заглушку правит karachur.tg.status, показывая в ней текущий
этап работы. Здесь про это знать незачем: отсюда заглушка уходит в чат и сюда же
возвращается за готовым текстом, а что с ней происходило между - дело того модуля.

Ошибка API уходит в чат полным текстом - человеку нужно видеть, что именно сломалось.
А вот в контекст модели вместо этой простыни с трейсбеком кладется короткая пометка
ERROR_CONTEXT_NOTE: длинный текст ошибки в истории только сбивает модель на следующем
запросе, полезной информации в нем для нее нет.
"""

import asyncio
import logging
import sqlite3

from telegram import Message
from telegram.error import TelegramError

from karachur.storage import messages
from karachur.text.html_splitter import split_html_message
from karachur.text.markdown import markdown_to_telegram_html

# Заглушка, которую бот шлет сразу и потом заменяет готовым ответом.
GENERATING_PLACEHOLDER = "⏳ Генерирую ответ..."
# В чат уходит полный текст ошибки, а в контекст модели - только эта короткая пометка.
ERROR_CONTEXT_NOTE = "ошибка gemini api"
# Пауза между кусками длинного ответа. Когда ответ разбит больше чем на четыре сообщения,
# они уходят в чат подряд одно за другим, и Telegram на такой частоте начинает отбивать
# запросы по лимиту сообщений в чат - без паузы часть кусков просто не доставляется.
CHUNK_PAUSE = 10

logger = logging.getLogger(__name__)


async def send_placeholder(message: Message) -> Message | None:
    """
    Отправляет сообщение-заглушку о начале генерации.

    :param message: сообщение пользователя, на которое отвечаем
    :type message: Message
    :return: отправленное сообщение или None, если отправить не удалось
    :rtype: Message | None
    """
    try:
        return await message.reply_text(GENERATING_PLACEHOLDER)
    except TelegramError as e:
        logger.warning("Не удалось отправить заглушку: %s", e)
        return None


async def replace_placeholder(
    placeholder: Message | None, original: Message, text: str
) -> Message:
    """
    Заменяет текст заглушки готовым ответом.

    Если отредактировать не вышло (заглушку удалили, истек срок правки), убираем ее
    и отвечаем обычным сообщением.

    :param placeholder: сообщение-заглушка или None, если ее не удалось отправить
    :type placeholder: Message | None
    :param original: сообщение пользователя, на которое отвечаем
    :type original: Message
    :param text: готовый текст ответа
    :type text: str
    :return: сообщение бота с итоговым текстом
    :rtype: Message
    """
    if placeholder is not None:
        try:
            edited = await placeholder.edit_text(text, parse_mode="HTML")
            # edit_text возвращает bool, если правим не свое сообщение - тогда берем исходное.
            return edited if isinstance(edited, Message) else placeholder
        except TelegramError as e:
            logger.warning("Не удалось отредактировать заглушку: %s", e)
            try:
                await placeholder.delete()
            except TelegramError as delete_error:
                logger.warning("Не удалось удалить заглушку: %s", delete_error)

    return await original.reply_text(text, parse_mode="HTML")


async def deliver_response(
    db_conn: sqlite3.Connection,
    message: Message,
    placeholder: Message | None,
    response_text: str,
    err: bool,
):
    """
    Отправляет готовый текст в чат и кладет его в контекст.

    Первый кусок заменяет заглушку, остальные уходят отдельными ответами.

    :param db_conn: соединение с базой данных
    :type db_conn: sqlite3.Connection
    :param message: сообщение пользователя, на которое отвечаем
    :type message: Message
    :param placeholder: сообщение-заглушка или None, если ее не удалось отправить
    :type placeholder: Message | None
    :param response_text: текст ответа модели или описание ошибки
    :type response_text: str
    :param err: True, если вместо ответа модели отправляем ошибку
    :type err: bool
    """
    message_chunks = [
        chunk
        for chunk in split_html_message(markdown_to_telegram_html(response_text), 3900)
        if chunk.strip()
    ] or [response_text]

    for index, chunk in enumerate(message_chunks):
        if index == 0:
            bot_reply = await replace_placeholder(placeholder, message, chunk)
        else:
            bot_reply = await message.reply_text(chunk, parse_mode="HTML")

        if len(message_chunks) > 4 and index < len(message_chunks) - 1:
            await asyncio.sleep(CHUNK_PAUSE)

        # Ответ модели сохраняем как есть, ошибку - одной короткой пометкой и один раз.
        if not err:
            messages.save_message_to_db(db_conn, bot_reply, is_bot=True)
        elif index == 0:
            messages.save_message_to_db(
                db_conn, bot_reply, is_bot=True, content_override=ERROR_CONTEXT_NOTE
            )
