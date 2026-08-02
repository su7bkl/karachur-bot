"""
Карачур Бот - Telegram бот, интегрированный с Google Gemini AI.

Этот бот реагирует на сообщения в групповых чатах или личных сообщениях,
которые начинаются с триггерного слова "Карачур". Бот сохраняет историю сообщений
в SQLite базе данных и может работать с различными типами медиа-файлов.
"""

# Модуль перерос порог pylint. Разносить его на пакеты - отдельная задача, пока живем так.
# pylint: disable=too-many-lines

import asyncio
import configparser
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone

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
        # Необязательный параметр: у старых конфигов его нет, поэтому с запасным значением.
        "MAX_CONTEXT_TOKENS": config.getint(
            "SETTINGS", "MAX_CONTEXT_TOKENS", fallback=200_000
        ),
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
MAX_CONTEXT_TOKENS = CONFIG["MAX_CONTEXT_TOKENS"]

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

# --- НАСТРОЙКИ СЖАТИЯ КОНТЕКСТА ---
# Когда история перестает влезать в MAX_CONTEXT_TOKENS, самая старая ее часть уходит
# модели на пересказ, а пересказ занимает ее место в контексте следующих запросов.
# Сжимаем с запасом: если целиться ровно в лимит, сжатие будет срабатывать почти на
# каждое сообщение. Доля от лимита, в которую хотим уложиться после сжатия.
CONTEXT_TARGET_RATIO = 0.5
# Столько последних сообщений остаются в контексте дословно при любом сжатии.
KEEP_RECENT_MESSAGES = 10
# Больше этого числа проходов сжатия за один ответ не делаем.
MAX_COMPRESSION_ROUNDS = 3
# Точный подсчет токенов - лишний запрос к API, поэтому сначала прикидываем размер на
# глаз и зовем count_tokens, только если грубая оценка подобралась к этой доле лимита.
TOKEN_CHECK_RATIO = 0.5
# Кириллица в токенайзере Gemini дает примерно 2-3 символа на токен, берем нижнюю границу.
CHARS_PER_TOKEN = 2.0
# Медиа в оценке считаем по верхней границе: недооценка дороже лишнего точного подсчета.
MEDIA_TOKEN_ESTIMATE = 2000

# --- СЛУЖЕБНЫЕ СООБЩЕНИЯ ---
# Заглушка, которую бот шлет сразу и потом заменяет готовым ответом.
GENERATING_PLACEHOLDER = "⏳ Генерирую ответ..."
# В чат уходит полный текст ошибки, а в контекст модели - только эта короткая пометка.
ERROR_CONTEXT_NOTE = "ошибка gemini api"
# Заголовок, под которым сжатая история уходит в контекст следующих запросов.
SUMMARY_HEADER = "[Сжатый пересказ более ранней части чата]"
# Задача на сжатие. Уходит первой репликой, чтобы история читалась уже с ней в голове.
SUMMARY_INSTRUCTION = (
    "[Служебная задача] Дальше идет начало истории группового чата, которое надо сжать, "
    "чтобы освободить место в контексте. Перескажи ее связным текстом на русском языке. "
    "Сохрани: кто участвовал и чем запомнился, о чем договорились, факты об участниках и "
    "чате, клички, шутки и отсылки, которые всплывают в разговоре, содержание присланных "
    "файлов и картинок, незакрытые вопросы и обещания. Опусти пустую болтовню и "
    'приветствия. Пиши по существу, без вступлений вроде "вот пересказ". Не отвечай на '
    "реплики из истории и не обращайся к участникам: это служебная выжимка для тебя же, "
    "а не сообщение в чат."
)
# Финальная реплика запроса на сжатие: историю уже показали, осталось попросить результат.
SUMMARY_REQUEST = "Сделай пересказ показанной истории по служебной задаче выше."

# --- СЛУЖЕБНЫЙ ПРЕФИКС АВТОРА ---
# Каждая реплика уходит в модель с пометкой вида "[Имя aka ник date:...]: ".
# Свои прошлые ответы модель видит с такой же пометкой и копирует ее в новый ответ.
# Лечим не запретом, а затравкой: пометку для будущей реплики бот пишет сам, модели
# остается только продолжить строку обычным текстом.
# Страховка на случай, если модель все же начнет ответ с копии пометки.
AUTHOR_TAG_PATTERN = re.compile(r"^\s*\[[^\]\n]*?date:[^\]\n]*\]\s*:?[ \t]*")

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
            is_bot BOOLEAN DEFAULT 0,
            summarized INTEGER DEFAULT 0
        )
    """)
    # Пересказы сжатых кусков истории. Сами сообщения остаются в messages, но в контекст
    # больше не попадают: их заменяет последний пересказ.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS context_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT,
            covered_messages INTEGER,
            created_at TEXT
        )
    """)
    add_summarized_column(cursor)
    conn.commit()
    return conn


def add_summarized_column(cursor: sqlite3.Cursor):
    """
    Дописывает колонку summarized в базы, созданные до появления сжатия.

    :param cursor: курсор открытой базы
    :type cursor: sqlite3.Cursor
    """
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(messages)")}
    if "summarized" not in columns:
        cursor.execute("ALTER TABLE messages ADD COLUMN summarized INTEGER DEFAULT 0")
        logger.info("В таблицу messages добавлена колонка summarized.")


def build_author_tag(name: str, username: str | None, date: str) -> str:
    """
    Собирает служебную подпись автора реплики: "Имя aka ник date:...".

    Единая точка сборки нужна, чтобы подпись сохраненного сообщения и подпись-затравка
    для ответа бота гарантированно совпадали по формату.

    :param name: отображаемое имя автора
    :type name: str
    :param username: ник в Telegram без "@" или None, если ника нет
    :type username: str | None
    :param date: дата реплики в том же виде, в каком ее хранит БД
    :type date: str
    :return: служебная подпись без квадратных скобок
    :rtype: str
    """
    tag = name
    if username:
        tag += f" aka {username}"
    return f"{tag} date:{date}"


def build_reply_prefix(bot) -> str:
    """
    Готовит затравку - начало будущей реплики бота в формате контекста.

    Эту строку мы подставляем последним сообщением роли "model", поэтому модель не
    придумывает служебный префикс заново, а просто продолжает его обычным текстом.

    :param bot: объект бота Telegram, у которого уже известны имя и ник
    :return: префикс вида "[Имя aka ник date:...]: "
    :rtype: str
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return f"[{build_author_tag(bot.full_name, bot.username, str(now))}]: "


def strip_author_tag(text: str) -> str:
    """
    Срезает служебный префикс автора, если модель все же продублировала его.

    :param text: текст ответа модели
    :type text: str
    :return: текст без ведущей служебной пометки
    :rtype: str
    """
    cleaned, replaced = AUTHOR_TAG_PATTERN.subn("", text, count=1)
    return cleaned.lstrip() if replaced else text


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
        date = (
            str(message.date)
            if not message.edit_date
            else str(message.date) + "/edited:" + str(message.edit_date)
        )
        user_prompt = build_author_tag(
            message.from_user.full_name or str(message.from_user.id),
            message.from_user.username,
            date,
        )
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


def get_latest_summary(conn: sqlite3.Connection) -> str | None:
    """
    Возвращает последний пересказ сжатой части истории.

    Каждый следующий пересказ вбирает в себя предыдущий, поэтому актуален всегда
    только самый свежий.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :return: текст пересказа или None, если сжатия еще не было
    :rtype: str | None
    """
    cursor = conn.cursor()
    cursor.execute("SELECT summary FROM context_summaries ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    return row[0] if row else None


def save_summary(conn: sqlite3.Connection, summary: str, message_ids: list):
    """
    Сохраняет пересказ и помечает вошедшие в него сообщения как сжатые.

    Помечаем поименно, а не по времени последнего сжатого сообщения: сдвиг часов или
    сообщение, пришедшее с запозданием, увели бы границу по времени не туда, и кусок
    истории молча выпал бы из контекста.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param summary: текст пересказа
    :type summary: str
    :param message_ids: message_id сообщений, вошедших в пересказ
    :type message_ids: list
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO context_summaries (summary, covered_messages, created_at)
        VALUES (?, ?, ?)
    """,
        (summary, len(message_ids), datetime.now(timezone.utc).isoformat()),
    )
    cursor.executemany(
        "UPDATE messages SET summarized = 1 WHERE message_id = ?",
        [(message_id,) for message_id in message_ids],
    )
    conn.commit()
    logger.info("Сохранен пересказ %d сообщений.", len(message_ids))


def get_context(conn: sqlite3.Connection) -> tuple[str | None, list]:
    """
    Получает контекст для модели: пересказ старой части истории и сообщения после нее.

    Пока сжатия не было, пересказ пуст и возвращается вся история.

    Args:
        conn (sqlite3.Connection): Соединение с базой данных.

    Returns:
        tuple: (текст пересказа или None, список словарей с информацией о сообщениях).
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM messages WHERE summarized = 0
        ORDER BY timestamp ASC, message_id ASC
    """)
    columns = [description[0] for description in cursor.description]
    messages = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return get_latest_summary(conn), messages


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


def build_message_parts(client: genai.Client, msg: dict) -> list:
    """
    Превращает одно сообщение из БД в части запроса к модели.

    :param client: клиент ИИ
    :type client: genai.Client
    :param msg: строка таблицы messages в виде словаря
    :type msg: dict
    :return: список частей (текст плюс медиа, если оно есть)
    :rtype: list
    """
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


def build_history(client: genai.Client, context_messages: list) -> list:
    """
    Готовит историю переписки в виде реплик для модели.

    Рядом с ролью и частями кладем исходную строку БД: по ней сжатие потом определяет,
    на каком сообщении провести границу.

    :param client: клиент ИИ
    :type client: genai.Client
    :param context_messages: список сообщений контекста
    :type context_messages: list
    :return: список словарей вида {"role", "parts", "source"}
    :rtype: list
    """
    history = []
    for msg in context_messages:
        parts = build_message_parts(client, msg)
        if parts:
            role = "model" if msg.get("is_bot") else "user"
            history.append({"role": role, "parts": parts, "source": msg})
    return history


def build_contents(history: list, summary: str | None, reply_prefix: str) -> list:
    """
    Собирает итоговый запрос: системный промпт, пересказ, история и затравка.

    :param history: история переписки от build_history (непустая)
    :type history: list
    :param summary: пересказ сжатой части истории или None
    :type summary: str | None
    :param reply_prefix: затравка - служебный префикс будущей реплики бота
    :type reply_prefix: str
    :return: содержимое запроса к модели
    :rtype: list
    """
    contents = [
        genai.types.ContentDict(
            role="user", parts=[genai.types.PartDict(text=SYSTEM_PROMPT)]
        )
    ]

    # Сжатая часть истории идет перед дословными сообщениями, в хронологическом порядке.
    if summary:
        contents.append(
            genai.types.ContentDict(
                role="user",
                parts=[genai.types.PartDict(text=f"{SUMMARY_HEADER}\n{summary}")],
            )
        )

    # Добавляем историю сообщений
    for entry in history[:-1]:
        contents.append(
            genai.types.ContentDict(role=entry["role"], parts=entry["parts"])
        )

    contents.append(genai.types.ContentDict(role="user", parts=history[-1]["parts"]))

    # Затравка: незакрытая реплика бота, которую модель должна продолжить. Служебный
    # префикс уже написан нами и в ответ модели не попадает, так что копировать его
    # в начало текста ей больше незачем.
    contents.append(
        genai.types.ContentDict(
            role="model", parts=[genai.types.PartDict(text=reply_prefix)]
        )
    )

    return contents


# --- БЛОК СЖАТИЯ КОНТЕКСТА ---


def estimate_entry_tokens(entry: dict) -> float:
    """
    Грубо оценивает размер одной реплики в токенах.

    :param entry: реплика из build_history
    :type entry: dict
    :return: примерное число токенов
    :rtype: float
    """
    total = 0.0
    for part in entry["parts"]:
        text = getattr(part, "text", None)
        total += len(text) / CHARS_PER_TOKEN if text else MEDIA_TOKEN_ESTIMATE
    return total


def estimate_context_tokens(history: list, summary: str | None) -> float:
    """
    Грубо оценивает размер всего контекста, чтобы не дергать API на каждое сообщение.

    :param history: история переписки от build_history
    :type history: list
    :param summary: пересказ сжатой части истории или None
    :type summary: str | None
    :return: примерное число токенов
    :rtype: float
    """
    total = len(SYSTEM_PROMPT) / CHARS_PER_TOKEN
    if summary:
        total += len(summary) / CHARS_PER_TOKEN
    return total + sum(estimate_entry_tokens(entry) for entry in history)


async def count_context_tokens(client: genai.Client, contents: list) -> int | None:
    """
    Считает точный размер запроса токенайзером Gemini.

    :param client: клиент ИИ
    :type client: genai.Client
    :param contents: подготовленное содержимое запроса
    :type contents: list
    :return: число токенов или None, если посчитать не вышло
    :rtype: int | None
    """
    try:
        # Вызов синхронный, уводим его в поток, чтобы не морозить event loop.
        response = await asyncio.to_thread(
            client.models.count_tokens, model=MODEL, contents=contents
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Не удалось посчитать токены контекста: %s", e)
        return None
    return getattr(response, "total_tokens", None)


def choose_cut_index(history: list, total_tokens: int) -> int:
    """
    Решает, сколько самых старых реплик отправить в пересказ.

    Отрезаем не половину наугад, а столько, чтобы остаток уложился в целевую долю
    лимита. Вес реплик берем оценочный: точные размеры кусков нам взять неоткуда,
    а промах компенсирует повторный проход сжатия.

    :param history: история переписки от build_history
    :type history: list
    :param total_tokens: точный размер контекста, который не влез в лимит
    :type total_tokens: int
    :return: сколько реплик с начала истории надо сжать (0 - сжимать нечего)
    :rtype: int
    """
    weights = [estimate_entry_tokens(entry) for entry in history]
    keep_weight = sum(weights) * (
        MAX_CONTEXT_TOKENS * CONTEXT_TARGET_RATIO / total_tokens
    )

    # Идем с конца и набираем хвост, который оставляем дословно.
    tail_weight = 0.0
    cut = len(history)
    for index in range(len(history) - 1, -1, -1):
        if tail_weight + weights[index] > keep_weight:
            break
        tail_weight += weights[index]
        cut = index

    # Свежие реплики не сжимаем никогда, даже если одна из них весит больше лимита.
    return min(cut, max(len(history) - KEEP_RECENT_MESSAGES, 0))


async def summarize_history(
    client: genai.Client, entries: list, previous_summary: str | None
) -> str:
    """
    Просит модель пересказать кусок истории одним текстом.

    Предыдущий пересказ идет в запрос вместе с историей, чтобы он не потерялся:
    новый пересказ заменяет его целиком.

    :param client: клиент ИИ
    :type client: genai.Client
    :param entries: реплики, которые надо сжать
    :type entries: list
    :param previous_summary: прошлый пересказ или None, если сжимаем впервые
    :type previous_summary: str | None
    :return: текст нового пересказа
    :rtype: str
    :raises GeminiRetryError: если модель так и не ответила
    """
    contents = [
        genai.types.ContentDict(
            role="user", parts=[genai.types.PartDict(text=SUMMARY_INSTRUCTION)]
        )
    ]
    if previous_summary:
        contents.append(
            genai.types.ContentDict(
                role="user",
                parts=[
                    genai.types.PartDict(text=f"{SUMMARY_HEADER}\n{previous_summary}")
                ],
            )
        )
    for entry in entries:
        contents.append(
            genai.types.ContentDict(role=entry["role"], parts=entry["parts"])
        )
    contents.append(
        genai.types.ContentDict(
            role="user", parts=[genai.types.PartDict(text=SUMMARY_REQUEST)]
        )
    )

    logger.info("Сжимаем %d самых старых сообщений в пересказ...", len(entries))
    return (await generate_with_retries(client, contents)).strip()


async def compress_context(
    client: genai.Client,
    conn: sqlite3.Connection,
    history: list,
    summary: str | None,
    reply_prefix: str,
) -> list:
    """
    Ужимает контекст до лимита и возвращает готовое содержимое запроса.

    Пока запрос не влезает в MAX_CONTEXT_TOKENS, самая старая часть истории уходит
    модели на пересказ, пересказ попадает в БД и занимает ее место. Один проход не
    всегда доводит до цели (пересказ тоже занимает место), поэтому проходов может
    быть несколько.

    Сжатие - это оптимизация, а не обязательный шаг: если посчитать токены или
    получить пересказ не вышло, отправляем контекст как есть и даем разбираться
    обычному механизму повторов.

    :param client: клиент ИИ
    :type client: genai.Client
    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param history: история переписки от build_history (непустая)
    :type history: list
    :param summary: пересказ сжатой ранее части истории или None
    :type summary: str | None
    :param reply_prefix: затравка - служебный префикс будущей реплики бота
    :type reply_prefix: str
    :return: содержимое запроса, влезающее в лимит (или максимально к нему близкое)
    :rtype: list
    """
    contents = build_contents(history, summary, reply_prefix)

    # Дешевая прикидка на входе: обычный чат до лимита не дотягивает, и тратить на него
    # лишний запрос к API незачем. Дальше по кругу идем уже только с точным подсчетом -
    # ошибись прикидка, и сжатие остановилось бы, не дойдя до лимита.
    if estimate_context_tokens(history, summary) < (
        MAX_CONTEXT_TOKENS * TOKEN_CHECK_RATIO
    ):
        return contents

    for _ in range(MAX_COMPRESSION_ROUNDS):
        total_tokens = await count_context_tokens(client, contents)
        if total_tokens is None:
            return contents
        if total_tokens <= MAX_CONTEXT_TOKENS:
            logger.info("Контекст: %d токенов из %d.", total_tokens, MAX_CONTEXT_TOKENS)
            return contents

        cut = choose_cut_index(history, total_tokens)
        if cut <= 0:
            logger.warning(
                "Контекст (%d токенов) больше лимита %d, но сжимать уже нечего.",
                total_tokens,
                MAX_CONTEXT_TOKENS,
            )
            return contents

        logger.info(
            "Контекст разросся до %d токенов при лимите %d, сжимаем.",
            total_tokens,
            MAX_CONTEXT_TOKENS,
        )
        try:
            summary = await summarize_history(client, history[:cut], summary)
        except GeminiRetryError as e:
            logger.error("Не удалось сжать контекст, отправляем как есть: %s", e)
            return contents

        save_summary(
            conn, summary, [entry["source"]["message_id"] for entry in history[:cut]]
        )
        history = history[cut:]
        contents = build_contents(history, summary, reply_prefix)

    logger.warning(
        "Контекст не уложился в лимит за %d проходов.", MAX_COMPRESSION_ROUNDS
    )
    return contents


async def generate_gemini_response(
    client: genai.Client,
    conn: sqlite3.Connection,
    context_messages: list,
    summary: str | None,
    reply_prefix: str,
):
    """
    Генерирует ответ с использованием модели Google Gemini AI на основе контекста сообщений.

    Args:
        client (genai.GenerativeModel): Клиент Google Gemini AI.
        conn (sqlite3.Connection): Соединение с БД - нужно, чтобы сохранить пересказ.
        context_messages (list): Список сообщений контекста.
        summary (str | None): Пересказ сжатой ранее части истории.
        reply_prefix (str): Затравка - служебный префикс будущей реплики бота.

    Returns:
        str: Сгенерированный ответ.
    """
    logger.info("Подготовка %d сообщений контекста для Gemini.", len(context_messages))
    history = build_history(client, context_messages)

    if not history:
        logger.warning("Контекст для Gemini пуст. Отмена запроса.")
        return "Не могу обработать пустой запрос."

    contents = await compress_context(client, conn, history, summary, reply_prefix)

    logger.info("Отправка запроса в Gemini...")

    # Генерируем ответ с новым API, повторяя попытки при сбоях
    response_text = await generate_with_retries(client, contents)
    return strip_author_tag(response_text)


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
        summary, context_messages = get_context(db_conn)
        gemini_client = context.bot_data["gemini_client"]

        if bool(message.voice) and not triggered_by_text:
            # Расшифровке чужая история не нужна - ни сообщения, ни пересказ.
            context_messages = context_messages[-1:]
            summary = None
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
                gemini_client,
                db_conn,
                context_messages,
                summary,
                build_reply_prefix(context.bot),
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
