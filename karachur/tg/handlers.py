"""
Обработчик обычных сообщений: от входящего Update до ответа модели в чате.

Здесь сходится все остальное - история в karachur.storage, разговор с моделью в
karachur.gemini, перекодирование вложений в karachur.media, отправка в karachur.tg.
delivery. Сам модуль не решает ни одной из этих задач, он только выстраивает их в
порядок и определяет, кому сейчас отвечать.

Порядок этот не произволен, и два его свойства держатся здесь намеренно.

Первое: сообщение попадает в базу ДО очереди за замком чата. Ждать своей очереди запрос
может минутами (повторы, смена ключей), и если бы запись шла после, история отставала бы
от чата ровно на это время - а следующий ответ собирался бы по неполной картине.

Второе: замок свой на каждый чат, а не один на бота. Внутри чата ответы обязаны идти по
очереди, иначе две реплики соберут контекст до того, как в него ляжет ответ на первую. А
вот разным чатам ждать друг друга незачем: один долгий запрос не должен превращаться в
очередь для всех остальных.

Третье: вложение, которое karachur.media.policy отвергает уже по mime и имени, сюда не
скачивается вовсе. Сообщение при этом сохраняется как обычно - пропадает только файл,
которому все равно нечего делать в запросе к модели.

Настройки обработчики берут из context.bot_data: сигнатуру задает telegram.ext, передать
в нее что-то свое нельзя, а bot_data - штатное место для общих данных бота.
"""

import asyncio
import logging
import os
import sqlite3

from telegram import Message, Update
from telegram.ext import ContextTypes

from karachur import config, media
from karachur.gemini import answer

# Модуль зовется key_pool, а не pool: имя pool по всему коду занято самим пулом чата
# (аргументы обработчиков, поле сессии), и модуль под тем же именем ими бы перекрывался.
from karachur.gemini import pool as key_pool
from karachur.media import paths, policy
from karachur.session import ChatSession
from karachur.storage import messages
from karachur.tg import delivery

logger = logging.getLogger(__name__)


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

    placeholder = await delivery.send_placeholder(message)
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

    await delivery.deliver_response(db_conn, message, placeholder, response_text, err)


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
        # Само сообщение в базе уже есть - пропустить можно только скачивание. Если
        # политика форматов отвергает вложение (архив, установщик, двоичный мусор),
        # качать его незачем: до модели оно все равно не доедет, а место на диске и
        # время на перекодирование займет. Решение тут принимается по одним mime и
        # имени - содержимого, по которому политика умеет разбираться дальше, до
        # скачивания просто нет, и ответ "не знаю" (None) означает "качай".
        if policy.decide_without_content(mime_type, file_name) is policy.Action.SKIP:
            logger.info(
                "Вложение %s (%s) модели не годится - не скачиваем.",
                file_name or file_id,
                mime_type,
            )
        else:
            file_path = paths.get_media_path(
                cfg.media_dir, file_id, mime_type, file_name
            )
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
