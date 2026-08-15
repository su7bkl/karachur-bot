"""
Карачур Бот - Telegram бот, интегрированный с Google Gemini AI.

Этот бот реагирует на сообщения в групповых чатах или личных сообщениях,
которые начинаются с триггерного слова "Карачур". Бот сохраняет историю сообщений
в SQLite базе данных и может работать с различными типами медиа-файлов.
"""

import asyncio
import logging
import os
import sqlite3
import time

from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import commands
from karachur import config, media
from karachur.gemini import answer

# Модуль зовется key_pool, а не pool: имя pool по всему коду занято самим пулом чата
# (аргументы обработчиков, поле сессии), и модуль под тем же именем ими бы перекрывался.
from karachur.gemini import pool as key_pool
from karachur.media import paths
from karachur.session import ChatSession
from karachur.storage import keys as key_store
from karachur.storage import messages, schema
from karachur.text.html_splitter import split_html_message
from karachur.text.markdown import markdown_to_telegram_html


# --- НАСТРОЙКИ ---
# Настройки живут в karachur.config и собираются в main(): дальше по коду они идут
# явными аргументами, а не модульными глобалами. Модульным здесь остается только то,
# что настройкой не является и никогда не меняется - служебные тексты.

# --- СЛУЖЕБНЫЕ СООБЩЕНИЯ ---
# Заглушка, которую бот шлет сразу и потом заменяет готовым ответом.
GENERATING_PLACEHOLDER = "⏳ Генерирую ответ..."
# В чат уходит полный текст ошибки, а в контекст модели - только эта короткая пометка.
ERROR_CONTEXT_NOTE = "ошибка gemini api"

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- БЛОК УТИЛИТ ДЛЯ МЕДИА ---


async def normalize_media(
    conn: sqlite3.Connection, message: Message, file_path: str, mime_type: str | None
):
    """
    Перекодирует скачанный файл под модель и запоминает, где он в итоге лег.

    Путь пишем в базу, потому что после ffmpeg у файла другое расширение: вычислить его
    по mime, как раньше, уже нельзя.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param message: сообщение, к которому относится файл
    :type message: Message
    :param file_path: путь к скачанному файлу
    :type file_path: str
    :param mime_type: mime, с которым файл пришел из Telegram
    :type mime_type: str | None
    """
    if not os.path.exists(file_path):
        return

    # Перекодирование блокирует надолго, уводим его в поток.
    path, mime = await asyncio.to_thread(media.normalize, file_path, mime_type)
    messages.set_media_path(conn, message.chat_id, message.message_id, path, mime)


# --- ГЛАВНЫЙ ОБРАЗОВАТЕЛЬ TELEGRAM ---


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

        if len(message_chunks) > 4:
            time.sleep(10)

        # Ответ модели сохраняем как есть, ошибку - одной короткой пометкой и один раз.
        if not err:
            messages.save_message_to_db(db_conn, bot_reply, is_bot=True)
        elif index == 0:
            messages.save_message_to_db(
                db_conn, bot_reply, is_bot=True, content_override=ERROR_CONTEXT_NOTE
            )


def chat_lock(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> asyncio.Lock:
    """
    Возвращает замок этого чата, заводя его при первом обращении.

    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    :param chat_id: идентификатор чата
    :type chat_id: int
    :return: замок, под которым чат готовит свой ответ
    :rtype: asyncio.Lock
    """
    locks = context.bot_data.setdefault("chat_locks", {})
    if chat_id not in locks:
        locks[chat_id] = asyncio.Lock()
    return locks[chat_id]


async def answer_chat(
    cfg: config.Config,
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    transcribe_only: bool,
):
    """
    Собирает контекст чата, спрашивает модель и отправляет ответ.

    :param cfg: настройки бота
    :type cfg: config.Config
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    :param message: сообщение, на которое отвечаем
    :type message: Message
    :param transcribe_only: это голосовое без триггера, нужна одна расшифровка
    :type transcribe_only: bool
    """
    db_conn = context.bot_data["db_conn"]
    chat_id = message.chat_id
    summary, context_messages = messages.get_context(db_conn, chat_id)

    if transcribe_only:
        # Расшифровке чужая история не нужна - ни сообщения, ни пересказ.
        context_messages = context_messages[-1:]
        summary = None
        if (
            context_messages
            and context_messages[-1].get("message_id") == message.message_id
        ):
            current_content = context_messages[-1].get("content", "")
            context_messages[-1]["content"] = (
                f"Напиши расшифровку голосового сообщения. {current_content}"
            )

    pool = ChatSession.create(cfg, db_conn, chat_id).pool

    placeholder = await send_placeholder(message)
    err = False

    try:
        response_text = await answer.generate_gemini_response(
            cfg, pool, db_conn, chat_id, context_messages, summary
        )
    except key_pool.NoUsableKeys as e:
        # Не поломка, а исчерпанный пул: человеку нужен не трейсбек, а что делать дальше.
        logger.warning("Чат %s остался без рабочего ключа: %s", chat_id, e)
        response_text = f"Не могу ответить: {e}"
        err = True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Ошибка при вызове Gemini API: %s", e)
        # В чат уходит полный текст ошибки вместе с числом попыток.
        response_text = f"Произошла ошибка при обращении к нейросети: {e}"
        err = True

    await deliver_response(db_conn, message, placeholder, response_text, err)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Главный обработчик сообщений Telegram.

    Настройки берутся из bot_data: сигнатуру обработчика задает telegram.ext, передать
    в нее что-то свое нельзя, а bot_data - штатное место для общих данных бота.

    :param update: объект обновления Telegram
    :type update: Update
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    """
    message = update.effective_message
    if not message or message.chat.type not in ("group", "supergroup", "private"):
        return

    cfg = context.bot_data["cfg"]
    db_conn = context.bot_data["db_conn"]
    trigger = cfg.trigger_word.lower()
    triggered_by_text = (
        message.text and message.text.lower().startswith(trigger)
    ) or (message.caption and message.caption.lower().startswith(trigger))

    file_id, mime_type, file_name = messages.save_message_to_db(
        db_conn, message, is_bot=False
    )
    if file_id:
        file_path = paths.get_media_path(cfg.media_dir, file_id, mime_type, file_name)
        if file_path:
            await paths.download_media_file(context.application, file_id, file_path)
            await normalize_media(db_conn, message, file_path, mime_type)

    if not (triggered_by_text or message.voice):
        return

    # Внутри чата ответы идут по очереди, а разные чаты друг друга не ждут: запрос к
    # модели с повторами растягивается на минуты, и одному чату незачем держать все
    # остальные. Сообщения при этом сохраняются сразу, до очереди, - история не отстает.
    async with chat_lock(context, message.chat_id):
        await answer_chat(
            cfg, context, message, bool(message.voice) and not triggered_by_text
        )


# --- ТОЧКА ВХОДА ---


# Команды бота: имя в Telegram и обработчик.
COMMAND_HANDLERS = {
    "help": commands.help_command,
    "start": commands.help_command,
    "keys": commands.keys_command,
    "addkey": commands.add_key_command,
    "delkey": commands.delete_key_command,
    "rotatekey": commands.rotate_key_command,
    "model": commands.model_command,
}


def main():
    """
    Основная функция запуска бота.

    Здесь и только здесь читается конфиг: дальше настройки идут по коду аргументами, а
    обработчикам достаются через bot_data. Поэтому импорт bot.py сам по себе ничего не
    читает с диска и не требует существующего config.cfg.
    """
    cfg = config.load_config()

    if not cfg.bot_token:
        raise ValueError("Пожалуйста, проверьте файл конфигурации: BOT_TOKEN не указан.")

    db_connection = schema.init_db(cfg.db_file, cfg.media_dir)

    # Ключ из конфига доступен всем чатам сразу; свои чат добавляет командой /addkey.
    key_store.sync_shared_key(db_connection, cfg.gemini_api_key)
    if not cfg.gemini_api_key:
        logger.info(
            "Общий ключ в config.cfg не задан - чаты работают только на своих ключах."
        )

    # Чаты обслуживаются параллельно: ответ с повторами занимает минуты, и один чат не
    # должен становиться очередью для всех остальных. Порядок внутри чата держит замок.
    application = (
        Application.builder().token(cfg.bot_token).concurrent_updates(True).build()
    )

    application.bot_data["db_conn"] = db_connection
    application.bot_data["cfg"] = cfg

    for name, handler in COMMAND_HANDLERS.items():
        application.add_handler(CommandHandler(name, handler))

    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
    )

    logger.info(
        "Модель по умолчанию: %s. Потолок запросов на ключ в сутки: %s.",
        cfg.model,
        cfg.key_rpd_limit or "не задан",
    )
    logger.info("Бот запускается...")
    application.run_polling()

    db_connection.close()
    logger.info("Соединение с БД закрыто.")


if __name__ == "__main__":
    main()
