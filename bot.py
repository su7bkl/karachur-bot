"""
Карачур Бот - Telegram бот, интегрированный с Google Gemini AI.

Этот бот реагирует на сообщения в групповых чатах или личных сообщениях,
которые начинаются с триггерного слова "Карачур". Бот сохраняет историю сообщений
в SQLite базе данных и может работать с различными типами медиа-файлов.
"""

import logging
import os
import sqlite3
import configparser
from datetime import datetime
import time

from telegram import Update, Message
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)
from google import genai


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
    cursor.execute(
        """
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
    """
    )
    conn.commit()
    return conn


def save_message_to_db(
    conn: sqlite3.Connection, message: Message, is_bot: bool = False
):
    """
    Сохраняет сообщение в базу данных.

    Args:
        conn (sqlite3.Connection): Соединение с базой данных.
        message (Message): Объект сообщения Telegram.
        is_bot (bool, optional): Флаг, указывающий, является ли сообщение от бота.
        По умолчанию False.

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

    timestamp = datetime.fromtimestamp(message.date.timestamp()).isoformat()
    reply_to_id = (
        message.reply_to_message.message_id if message.reply_to_message else None
    )
    user_id = message.from_user.id if message.from_user else None
    username = message.from_user.username if message.from_user else "Bot"

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
            username,
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
    if original_name:
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

uploaded_files = {}


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
            media_path = ".\\" + get_media_path(
                msg["file_id"], msg["mime_type"], msg.get("file_name")
            )
            if media_path and os.path.exists(media_path):
                try:
                    file_size = os.path.getsize(media_path)
                    if file_size < 20 * 1024 * 1024:  # 20 МБ
                        if media_path not in uploaded_files:
                            uploaded_files[media_path] = client.files.upload(
                                file=media_path
                            )
                            time.sleep(
                                FILE_UPLOAD_DELAY_PER_MB * (file_size / (1024 * 1024))
                            )  # Задержка для больших файлов
                        parts.append(
                            genai.types.Part(
                                file_data=genai.types.FileData(
                                    file_uri=uploaded_files[media_path].uri,
                                    mime_type=uploaded_files[media_path].mime_type,
                                )
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
                except (OSError, IOError) as e:
                    logger.error(
                        "Не удалось прочитать медиафайл %s для контекста: %s",
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

    # Генерируем ответ с новым API
    response = client.models.generate_content(model=MODEL, contents=contents)
    return response.text


# --- ГЛАВНЫЙ ОБРАЗОВАТЕЛЬ TELEGRAM ---


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    db_conn = context.bot_data["db_conn"]
    is_trigger = (
        message.text and message.text.lower().startswith(TRIGGER_WORD.lower())
    ) or (message.caption and message.caption.lower().startswith(TRIGGER_WORD.lower()))

    file_id, mime_type, file_name = save_message_to_db(db_conn, message, is_bot=False)
    if file_id:
        file_path = get_media_path(file_id, mime_type, file_name)
        if file_path:
            await download_media_file(context.application, file_id, file_path)

    if is_trigger:
        context_messages = get_context(db_conn)
        gemini_client = context.bot_data["gemini_client"]
        try:
            response_text = await generate_gemini_response(
                gemini_client, context_messages
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Ошибка при вызове Gemini API: %s", e)
            response_text = f"Произошла ошибка при обращении к нейросети: {e}"

        bot_reply = await message.reply_text(response_text)
        save_message_to_db(db_conn, bot_reply, is_bot=True)


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
