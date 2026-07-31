"""
Карачур Бот - Telegram бот, интегрированный с Google Gemini AI.

Этот бот реагирует на сообщения в групповых чатах или личных сообщениях,
которые начинаются с триггерного слова "Карачур". Бот сохраняет историю сообщений
в SQLite базе данных и может работать с различными типами медиа-файлов.
"""

import asyncio
import configparser
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime

from google import genai
from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from html_splitter import split_html_message
from markdown_converter import markdown_to_telegram_html


# --- ЧТЕНИЕ НАСТРОЕК ---
def load_config(config_path="config.cfg"):
    """
    Загружает настройки из конфигурационного файла (UTF-8).

    Args:
        config_path (str): Путь к файлу конфигурации.

    Returns:
        dict: Словарь с настройками.
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Файл конфигурации не найден: {config_path}")

    config = configparser.ConfigParser()
    with open(config_path, "r", encoding="utf-8") as f:
        config.read_file(f)

    settings = {
        "BOT_TOKEN": config.get("SETTINGS", "BOT_TOKEN"),
        "GEMINI_API_KEY": config.get("SETTINGS", "GEMINI_API_KEY"),
        "DB_FILE": config.get("SETTINGS", "DB_FILE"),
        "MEDIA_DIR": config.get("SETTINGS", "MEDIA_DIR"),
        "TRIGGER_WORD": config.get("SETTINGS", "TRIGGER_WORD"),
        "SYSTEM_PROMPT": config.get("SETTINGS", "SYSTEM_PROMPT"),
        "MODEL": config.get("SETTINGS", "MODEL"),
    }

    return settings


# Загружаем настройки
FILE_UPLOAD_DELAY_PER_MB = 0.6
CONFIG = load_config()
BOT_TOKEN = CONFIG["BOT_TOKEN"]
GEMINI_API_KEY = CONFIG["GEMINI_API_KEY"]
DB_FILE = CONFIG["DB_FILE"]
MEDIA_DIR = CONFIG["MEDIA_DIR"]
TRIGGER_WORD = CONFIG["TRIGGER_WORD"]
SYSTEM_PROMPT = CONFIG["SYSTEM_PROMPT"]
MODEL = CONFIG["MODEL"]

# --- НАСТРОЙКИ ПОВТОРНЫХ ПОПЫТОК ---
# Сколько раз пробуем получить от модели корректный текст, прежде чем сдаться.
MAX_RETRIES = 15
# Пауза растет экспоненциально (2, 4, 8, ...) до потолка. Суммарно ~18 минут.
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 120.0
# Ошибки, которые сами не пройдут: кривой запрос, битый ключ, нет прав, нет модели.
NON_RETRYABLE_CODES = frozenset({400, 401, 403, 404})
# В деталях ошибки 429 Gemini присылает рекомендованную паузу: "retryDelay": "27s".
RETRY_DELAY_PATTERN = re.compile(
    r"retry[-_]?delay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)s", re.IGNORECASE
)

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

# --- БЛОК РАБОТЫ С БАЗОЙ ДАННЫХ ---


def init_db():
    """
    Инициализирует базу данных SQLite и создает необходимые таблицы.

    Returns:
        sqlite3.Connection: Соединение с базой данных.
    """
    os.makedirs(MEDIA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE,
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
            is_bot BOOLEAN DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def save_message_to_db(
    conn: sqlite3.Connection,
    message: Message,
    is_bot: bool = False,
    content_override: str | None = None,
):
    """
    Сохраняет сообщение в базу данных.

    Args:
        conn (sqlite3.Connection): Соединение с базой данных.
        message (Message): Объект сообщения Telegram.
        is_bot (bool, optional): Флаг, указывающий, является ли сообщение от бота.
        По умолчанию False.
        content_override (str | None, optional): Текст, который попадет в контекст вместо
        реального текста сообщения. Нужен, чтобы простыня с ошибкой API не засоряла историю.

    Returns:
        tuple: (file_id, mime_type, file_name) - информация о медиа-файле, если он присутствует.
    """
    cursor = conn.cursor()
    content = message.text or message.caption or ""
    if not is_bot and content.lower().startswith(TRIGGER_WORD.lower()):
        content = content[len(TRIGGER_WORD) :]

    media_type, mime_type, file_id, file_name = None, None, None, None

    if message.photo:
        media_type, file_id, mime_type = (
            "photo",
            message.photo[-1].file_id,
            "image/jpeg",
        )
    elif message.document:
        media_type, file_id, mime_type, file_name = (
            "document",
            message.document.file_id,
            message.document.mime_type,
            message.document.file_name,
        )
    elif message.sticker:
        media_type, file_id = "sticker", message.sticker.file_id
        mime_type = (
            "image/webp"
            if not message.sticker.is_animated and not message.sticker.is_video
            else "video/webm"
        )
    elif message.video:
        media_type, file_id, mime_type, file_name = (
            "video",
            message.video.file_id,
            message.video.mime_type,
            message.video.file_name,
        )
    elif message.audio:
        media_type, file_id, mime_type, file_name = (
            "audio",
            message.audio.file_id,
            message.audio.mime_type,
            message.audio.file_name,
        )
    elif message.voice:
        media_type, file_id, mime_type = "audio", message.voice.file_id, "audio/ogg"
        content = f"[Голосовое сообщение by {message.from_user.username}]"
    elif message.video_note:
        media_type, file_id, mime_type = (
            "video",
            message.video_note.file_id,
            "video/mp4",
        )
        content = f"[Видео сообщение by {message.from_user.username}]"

    if content_override is not None:
        content = content_override

    timestamp = datetime.fromtimestamp(message.date.timestamp()).isoformat()
    reply_to_id = (
        message.reply_to_message.message_id if message.reply_to_message else None
    )
    user_id = message.from_user.id if message.from_user else None
    if message.from_user:
        user_prompt = (
            message.from_user.full_name
            if message.from_user.full_name
            else str(message.from_user.id)
        )
        if message.from_user.username:
            user_prompt += f" aka {message.from_user.username}"
        date = (
            str(message.date)
            if not message.edit_date
            else str(message.date) + "/edited:" + str(message.edit_date)
        )
        user_prompt += f" date:{date}"
    else:
        user_prompt = "Bot"

    cursor.execute(
        """
        INSERT OR REPLACE INTO messages (
            message_id, chat_id, user_id, username, content, media_type,
            mime_type, file_id, file_name, timestamp, reply_to_message_id, is_bot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            message.message_id,
            message.chat_id,
            user_id,
            user_prompt,
            content,
            media_type,
            mime_type,
            file_id,
            file_name,
            timestamp,
            reply_to_id,
            is_bot,
        ),
    )
    conn.commit()
    logger.info("Сохранено сообщение %s в БД.", message.message_id)  # lazy logging
    return file_id, mime_type, file_name


def get_context(conn: sqlite3.Connection):
    """
    Получает все сообщения из базы данных, отсортированные по времени.

    Args:
        conn (sqlite3.Connection): Соединение с базой данных.

    Returns:
        list: Список словарей, содержащих информацию о сообщениях.
    """
    cursor = conn.cursor()
    query = """
        SELECT * FROM messages ORDER BY timestamp ASC
    """
    cursor.execute(query)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# --- БЛОК УТИЛИТ ДЛЯ МЕДИА ---

uploaded_files = {}


def get_extension_from_mime(mime: str | None) -> str:
    """
    Определяет расширение файла по его MIME-типу.

    Args:
        mime (str | None): MIME-тип файла.

    Returns:
        str: Расширение файла.
    """
    if not mime:
        return "bin"
    mime_map = {
        "jpeg": "jpg",
        "png": "png",
        "gif": "gif",
        "webp": "webp",
        "ogg": "ogg",
        "mp4": "mp4",
        "mpeg": "mp3",
        "pdf": "pdf",
        "webm": "webm",
    }
    for key, value in mime_map.items():
        if key in mime.lower():
            return value
    return mime.split("/")[-1]


def get_media_path(
    file_id: str, mime_type: str | None, original_name: str | None
) -> str | None:
    """
    Формирует путь к файлу для сохранения медиа.

    Args:
        file_id (str): Идентификатор файла в Telegram.
        mime_type (str | None): MIME-тип файла.
        original_name (str | None, optional): Оригинальное имя файла.

    Returns:
        str | None: Путь к файлу или None, если файл не может быть сохранен.
    """
    if original_name and original_name.isascii():
        safe_name = "".join(
            c for c in original_name if c.isalnum() or c in (" ", ".", "_", "-")
        ).strip()
        return os.path.join(MEDIA_DIR, safe_name)
    if file_id:
        ext = get_extension_from_mime(mime_type)
        return os.path.join(MEDIA_DIR, f"{file_id}.{ext}")
    return None


async def download_media_file(application: Application, file_id: str, file_path: str):
    """
    Загружает медиа-файл из Telegram.

    Args:
        application (Application): Объект приложения Telegram.
        file_id (str): Идентификатор файла в Telegram.
        file_path (str): Путь для сохранения файла.
    """
    if os.path.exists(file_path):
        return
    try:
        logger.info("Загрузка файла %s в %s...", file_id, file_path)  # lazy logging
        tg_file = await application.bot.get_file(file_id)
        await tg_file.download_to_drive(file_path)
        logger.info("Файл успешно загружен: %s", file_path)  # lazy logging
    except (OSError, IOError) as e:
        logger.error("Ошибка загрузки файла %s: %s", file_id, e)  # lazy logging


# --- БЛОК ИНТЕГРАЦИИ С GEMINI ---


def check_file_validity(client: genai.Client, media_path: str):
    """
    Проверяет валидность файла

    :param client: клиент ИИ
    :type client: genai.Client
    :param media_path: путь к файлу
    :type media_path: str
    """
    if media_path in uploaded_files:
        try:
            remote_file = client.files.get(name=uploaded_files[media_path].name)
            if remote_file.state.name != "ACTIVE":
                logger.info(
                    "Файл %s в состоянии %s, требуется перевыгрузка",
                    media_path,
                    remote_file.state.name,
                )
                del uploaded_files[media_path]
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Не удалось проверить статус файла %s, перевыгружаем: %s",
                media_path,
                e,
            )
            del uploaded_files[media_path]


def upload_file(client: genai.Client, media_path):
    """
    Загружает файл

    :param client: клиент ИИ
    :type client: genai.Client
    :param media_path: путь к файлу
    :type media_path: str
    """
    uploaded_file = client.files.upload(file=media_path)

    # Цикл ожидания перехода в рабочее состояние
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state.name == "ACTIVE":
        uploaded_files[media_path] = uploaded_file
    else:
        logger.error(
            "Файл %s после загрузки перешел в состояние %s",
            media_path,
            uploaded_file.state.name,
        )


class GeminiRetryError(Exception):
    """Не удалось получить корректный ответ от Gemini за отведенное число попыток."""


def get_error_code(exc: Exception) -> int | None:
    """
    Определяет HTTP-код ошибки Gemini API.

    :param exc: пойманное исключение
    :type exc: Exception
    :return: код ответа или None, если определить не удалось (например, обрыв связи)
    :rtype: int | None
    """
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    # В разных версиях SDK текст ошибки начинается с кода: "429 RESOURCE_EXHAUSTED ..."
    match = re.match(r"\s*(\d{3})\b", str(exc))
    return int(match.group(1)) if match else None


def is_retryable_error(exc: Exception) -> bool:
    """
    Решает, имеет ли смысл повторять запрос после этой ошибки.

    :param exc: пойманное исключение
    :type exc: Exception
    :return: True, если ошибка похожа на временную
    :rtype: bool
    """
    code = get_error_code(exc)
    if code is None:
        # Таймауты и обрывы связи кода не имеют - их повторяем.
        return True
    return code not in NON_RETRYABLE_CODES


def get_backoff_delay(attempt: int, exc: Exception | None = None) -> float:
    """
    Считает паузу перед следующей попыткой.

    :param attempt: номер только что провалившейся попытки (начиная с единицы)
    :type attempt: int
    :param exc: исключение, если попытка упала с ошибкой API
    :type exc: Exception | None
    :return: длительность паузы в секундах
    :rtype: float
    """
    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
    if exc is not None:
        # Если API сам сказал, сколько ждать (429), слушаемся его.
        match = RETRY_DELAY_PATTERN.search(str(exc))
        if match:
            delay = float(match.group(1))
    delay = min(delay, RETRY_MAX_DELAY)
    # Джиттер, чтобы повторы не выстраивались в ровную сетку.
    return delay + random.uniform(0, delay * 0.1)


def extract_response_text(response) -> tuple[str | None, str, bool]:
    """
    Достает текст из ответа Gemini и объясняет, если текста нет.

    :param response: ответ метода generate_content
    :return: (текст или None, описание проблемы, стоит ли повторять запрос)
    :rtype: tuple[str | None, str, bool]
    """
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(feedback, "block_reason", None) if feedback else None
    if block_reason:
        # Блокировка самого запроса детерминирована - повтор ничего не изменит.
        return None, f"запрос заблокирован фильтрами ({block_reason})", False

    try:
        text = response.text
    except Exception as e:  # pylint: disable=broad-exception-caught
        return None, f"не удалось прочитать текст ответа: {e}", True

    if text and text.strip():
        return text, "", True

    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    return None, f"модель вернула пустой текст (finish_reason={finish})", True


async def generate_with_retries(client: genai.Client, contents: list) -> str:
    """
    Запрашивает ответ у Gemini, повторяя попытки при сбоях и пустых ответах.

    Повторяет до MAX_RETRIES раз с экспоненциально растущей паузой. Не повторяет
    ошибки, которые сами не исправятся (неверный ключ, недоступная модель,
    некорректный запрос), и блокировку запроса фильтрами безопасности.

    :param client: клиент ИИ
    :type client: genai.Client
    :param contents: подготовленное содержимое запроса
    :type contents: list
    :return: текст ответа модели
    :rtype: str
    :raises GeminiRetryError: если попытки исчерпаны
    """
    last_reason = "причина неизвестна"

    for attempt in range(1, MAX_RETRIES + 1):
        failure = None
        try:
            # Вызов синхронный, уводим его в поток, чтобы не морозить event loop.
            response = await asyncio.to_thread(
                client.models.generate_content, model=MODEL, contents=contents
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            if not is_retryable_error(e):
                logger.error(
                    "Неустранимая ошибка Gemini (код %s): %s", get_error_code(e), e
                )
                raise GeminiRetryError(
                    f"Неустранимая ошибка API на попытке {attempt} из {MAX_RETRIES} "
                    f"(код {get_error_code(e)}): {e}"
                ) from e
            failure = e
            last_reason = f"ошибка API {get_error_code(e)}: {e}"
        else:
            text, reason, can_retry = extract_response_text(response)
            if text:
                if attempt > 1:
                    logger.info(
                        "Ответ получен с попытки %d из %d.", attempt, MAX_RETRIES
                    )
                return text
            if not can_retry:
                logger.error("Повтор бесполезен: %s", reason)
                raise GeminiRetryError(
                    f"Повтор бесполезен, остановились на попытке {attempt} "
                    f"из {MAX_RETRIES}: {reason}"
                )
            last_reason = reason

        logger.warning(
            "Попытка %d из %d не удалась: %s", attempt, MAX_RETRIES, last_reason
        )

        if attempt < MAX_RETRIES:
            delay = get_backoff_delay(attempt, failure)
            logger.info("Повтор через %.1f с.", delay)
            await asyncio.sleep(delay)

    raise GeminiRetryError(
        f"Не удалось получить ответ за {MAX_RETRIES} попыток. Последняя причина: {last_reason}"
    )


async def generate_gemini_response(client: genai.Client, context_messages: list):
    """
    Генерирует ответ с использованием модели Google Gemini AI на основе контекста сообщений.

    Args:
        client (genai.GenerativeModel): Клиент Google Gemini AI.
        context_messages (list): Список сообщений контекста.

    Returns:
        str: Сгенерированный ответ.
    """
    history = []
    logger.info("Подготовка %d сообщений контекста для Gemini.", len(context_messages))

    # --- Декомпозиция: вынесем обработку одного сообщения в отдельную функцию ---

    def process_context_message(msg):
        parts = []
        author = msg.get("username") or ("Bot" if msg.get("is_bot") else "unknown")
        if msg.get("content"):
            parts.append(genai.types.Part(text=f"[{author}]: {msg.get('content')}"))
        else:
            parts.append(genai.types.Part(text=f"[{author}]"))

        if msg.get("file_id") and msg.get("mime_type"):
            raw_path = get_media_path(
                msg["file_id"], msg["mime_type"], msg.get("file_name")
            )
            media_path = os.path.abspath(raw_path) if raw_path else None
            if media_path and os.path.exists(media_path):
                try:
                    file_size = os.path.getsize(media_path)
                    if file_size < 20 * 1024 * 1024:
                        # Проверяем, есть ли файл в кэше и валиден ли он
                        check_file_validity(client, media_path)

                        # Загрузка, если файла нет в кэше (или он был удален выше)
                        if media_path not in uploaded_files:
                            upload_file(client, media_path)

                        # Если файл успешно загружен и активен
                        if media_path in uploaded_files:
                            parts.append(
                                genai.types.Part(
                                    file_data=genai.types.FileData(
                                        file_uri=uploaded_files[media_path].uri,
                                        mime_type=uploaded_files[media_path].mime_type,
                                    )
                                )
                            )
                        else:
                            parts.append(
                                genai.types.Part(
                                    text="[Ошибка обработки файла - не удалось активировать]"
                                )
                            )
                    else:
                        logger.warning(
                            "Файл %s слишком большой (%.2f МБ), пропускаем",
                            media_path,
                            file_size / 1024 / 1024,
                        )
                        parts.append(
                            genai.types.Part(
                                text="[Файл слишком большой для обработки - пропущено]"
                            )
                        )
                except Exception as e:  # pylint: disable=broad-exception-caught
                    logger.error(
                        "Ошибка при работе с медиафайлом %s: %s",
                        media_path,
                        e,
                    )
        return parts

    for msg in context_messages:
        parts = process_context_message(msg)
        if parts:
            role = "model" if msg.get("is_bot") else "user"
            history.append({"role": role, "parts": parts})

    if not history:
        logger.warning("Контекст для Gemini пуст. Отмена запроса.")
        return "Не могу обработать пустой запрос."

    # Создаем содержимое для генерации
    contents = []

    # Добавляем системный промпт
    contents.append(
        genai.types.ContentDict(
            role="user", parts=[genai.types.PartDict(text=SYSTEM_PROMPT)]
        )
    )

    # Добавляем историю сообщений
    for entry in history[:-1]:
        contents.append(
            genai.types.ContentDict(role=entry["role"], parts=entry["parts"])
        )

    contents.append(genai.types.ContentDict(role="user", parts=history[-1]["parts"]))

    logger.info("Отправка запроса в Gemini...")

    # Генерируем ответ с новым API, повторяя попытки при сбоях
    return await generate_with_retries(client, contents)


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
            save_message_to_db(db_conn, bot_reply, is_bot=True)
        elif index == 0:
            save_message_to_db(
                db_conn, bot_reply, is_bot=True, content_override=ERROR_CONTEXT_NOTE
            )


async def handle_message(  # pylint: disable=too-many-locals
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """
    Главный обработчик сообщений Telegram.

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст обработчика.
    """
    message = update.effective_message
    if not message or (
        message.chat.type not in ("group", "supergroup")
        and not message.chat.type == "private"
    ):
        return

    err = False

    db_conn = context.bot_data["db_conn"]
    triggered_by_text = (
        message.text and message.text.lower().startswith(TRIGGER_WORD.lower())
    ) or (message.caption and message.caption.lower().startswith(TRIGGER_WORD.lower()))

    file_id, mime_type, file_name = save_message_to_db(db_conn, message, is_bot=False)
    if file_id:
        file_path = get_media_path(file_id, mime_type, file_name)
        if file_path:
            await download_media_file(context.application, file_id, file_path)

    if triggered_by_text or bool(message.voice):
        context_messages = get_context(db_conn)
        gemini_client = context.bot_data["gemini_client"]

        if bool(message.voice) and not triggered_by_text:
            context_messages = context_messages[-1:]
            if (
                context_messages
                and context_messages[-1].get("message_id") == message.message_id
            ):
                current_content = context_messages[-1].get("content", "")
                context_messages[-1][
                    "content"
                ] = f"Напиши расшифровку голосового сообщения. {current_content}"

        placeholder = await send_placeholder(message)

        try:
            response_text = await generate_gemini_response(
                gemini_client, context_messages
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Ошибка при вызове Gemini API: %s", e)
            # В чат уходит полный текст ошибки вместе с числом попыток.
            response_text = f"Произошла ошибка при обращении к нейросети: {e}"
            err = True

        await deliver_response(db_conn, message, placeholder, response_text, err)


# --- ТОЧКА ВХОДА ---


def main():
    """
    Основная функция запуска бота.
    Инициализирует подключения к базе данных и API, настраивает обработчики сообщений.
    """
    if not BOT_TOKEN or not GEMINI_API_KEY:
        raise ValueError(
            "Пожалуйста, проверьте файл конфигурации. BOT_TOKEN или GEMINI_API_KEY не указаны."
        )

    db_connection = init_db()

    # Новый способ конфигурации клиента
    client = genai.Client(api_key=GEMINI_API_KEY)

    application = Application.builder().token(BOT_TOKEN).build()

    application.bot_data["db_conn"] = db_connection
    application.bot_data["gemini_client"] = client

    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
    )

    logger.info("Бот запускается...")
    application.run_polling()

    db_connection.close()
    logger.info("Соединение с БД закрыто.")


if __name__ == "__main__":
    main()
