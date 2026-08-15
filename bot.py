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
from telegram import (
    Message,
    MessageOrigin,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import api_keys
import chat_settings
import commands
from html_splitter import split_html_message
from markdown_converter import markdown_to_telegram_html


# --- ЧТЕНИЕ НАСТРОЕК ---
# Путь к конфигу можно задать переменной окружения: бота запускают и не из его папки,
# а тестам нужен свой конфиг, не трогающий рабочий.
CONFIG_ENV_VAR = "KARACHUR_CONFIG"


def load_config(config_path=None):
    """
    Загружает настройки из конфигурационного файла (UTF-8).

    Args:
        config_path (str | None): Путь к файлу конфигурации. Если не задан, берется из
        переменной окружения KARACHUR_CONFIG, а по умолчанию - config.cfg рядом с ботом.

    Returns:
        dict: Словарь с настройками.
    """
    if config_path is None:
        config_path = os.environ.get(CONFIG_ENV_VAR, "config.cfg")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Файл конфигурации не найден: {config_path}")

    config = configparser.ConfigParser()
    with open(config_path, "r", encoding="utf-8") as f:
        config.read_file(f)

    settings = {
        "BOT_TOKEN": config.get("SETTINGS", "BOT_TOKEN"),
        "DB_FILE": config.get("SETTINGS", "DB_FILE"),
        "MEDIA_DIR": config.get("SETTINGS", "MEDIA_DIR"),
        "TRIGGER_WORD": config.get("SETTINGS", "TRIGGER_WORD"),
        "SYSTEM_PROMPT": config.get("SETTINGS", "SYSTEM_PROMPT"),
        "MODEL": config.get("SETTINGS", "MODEL"),
        # Необязательные параметры: у старых конфигов их нет, поэтому с запасными значениями.
        # Ключ из конфига необязателен: чат может обойтись своими, добавленными /addkey.
        "GEMINI_API_KEY": config.get("SETTINGS", "GEMINI_API_KEY", fallback="").strip(),
        "MAX_CONTEXT_TOKENS": config.getint(
            "SETTINGS", "MAX_CONTEXT_TOKENS", fallback=200_000
        ),
        "KEY_RPD_LIMIT": config.getint("SETTINGS", "KEY_RPD_LIMIT", fallback=250),
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
KEY_RPD_LIMIT = CONFIG["KEY_RPD_LIMIT"]

# --- НАСТРОЙКИ ПОВТОРНЫХ ПОПЫТОК ---
# Сколько раз пробуем получить от модели корректный текст, прежде чем сдаться.
MAX_RETRIES = 15
# Пауза растет экспоненциально (2, 4, 8, ...) до потолка. Суммарно ~18 минут.
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 120.0
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
# Чужие реплики уходят в модель с пометкой вида "[Имя aka ник date:...]: " - без нее в
# групповом чате не разобрать, кто что сказал. Свои прошлые ответы модель видит уже без
# пометки: роль "model" и так говорит, чьи они, а с пометкой модель копировала ее в
# начало нового ответа. Затравкой - незакрытой репликой роли "model" - эту проблему
# лечить нельзя: запрос, который заканчивается репликой модели, Gemini отклоняет
# неустранимой ошибкой 400.
# Страховка на случай, если модель все же начнет ответ с копии пометки.
AUTHOR_TAG_PATTERN = re.compile(r"^\s*\[[^\]\n]*?date:[^\]\n]*\]\s*:?[ \t]*")

# --- СЛУЖЕБНЫЕ ПОМЕТКИ ПЕРЕД РЕПЛИКОЙ ---
# В контексте реплика-ответ оторвана от своего адресата: между ними может лежать сколько
# угодно чужих сообщений, а сам адресат - уже уйти в пересказ. Поэтому перед текстом такой
# реплики идет пометка с автором и началом того сообщения, на которое отвечают, и с
# выделенным фрагментом, если отвечающий процитировал кусок текста. Свои прошлые ответы
# модель, как и раньше, видит вообще без служебных пометок.
REPLY_NOTE_LEAD = "В ответ на"
QUOTE_NOTE_LEAD = "Процитирован фрагмент"
# Пересланное сообщение приходит от того, кто нажал "переслать": в from_user стоит он, а в
# тексте лежат чужие слова. Без пометки модель припишет их переславшему. Настоящий автор
# лежит в forward_origin, оттуда же берем и время оригинала: message.date у пересланного -
# это момент пересылки.
FORWARD_NOTE_LEAD = "Переслано"
# Сколько символов сообщения-адресата показываем: пометка должна давать его опознать,
# а не пересказывать целиком - иначе популярное сообщение размножится по всему контексту.
REPLY_SNIPPET_LIMIT = 300
# Цитату Telegram режет на своей стороне (около 1024 символов), порог тут - страховка.
QUOTE_SNIPPET_LIMIT = 1024
# Страховка на случай, если модель начнет ответ с копии пометки.
SERVICE_NOTE_PATTERN = re.compile(
    rf"^\s*\[(?:{FORWARD_NOTE_LEAD}|{REPLY_NOTE_LEAD}|{QUOTE_NOTE_LEAD})[^\n]*\]\s*"
)

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
    check_legacy_schema(cursor)
    # message_id уникален только внутри чата: в двух разных чатах номера повторяются, и
    # без chat_id в ограничении сообщения одного чата затирали бы сообщения другого.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
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
            quote_text TEXT,
            forward_origin TEXT,
            is_bot BOOLEAN DEFAULT 0,
            summarized INTEGER DEFAULT 0,
            UNIQUE (chat_id, message_id)
        )
    """)
    # Контекст собирается по одному чату, и почти всегда - по его несжатой части.
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS messages_chat_context
        ON messages (chat_id, summarized, timestamp)
    """)
    # Пересказы сжатых кусков истории. Сами сообщения остаются в messages, но в контекст
    # больше не попадают: их заменяет последний пересказ своего чата.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS context_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            summary TEXT,
            covered_messages INTEGER,
            created_at TEXT
        )
    """)
    api_keys.init_key_tables(cursor)
    chat_settings.init_settings_table(cursor)
    conn.commit()
    return conn


def check_legacy_schema(cursor: sqlite3.Cursor):
    """
    Отказывается работать с базой, созданной до разделения истории по чатам.

    В старой схеме message_id уникален сам по себе, а пересказы не привязаны к чату:
    подпереть это ALTER TABLE нельзя, а молча продолжить - значит перемешать истории
    разных чатов. Поэтому просто говорим, что делать.

    :param cursor: курсор открытой базы
    :type cursor: sqlite3.Cursor
    :raises RuntimeError: если база сделана прошлой версией бота
    """
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
    ).fetchone()
    if not row or not row[0]:
        return

    schema = " ".join(row[0].split()).lower()
    if "unique (chat_id, message_id)" in schema:
        return

    raise RuntimeError(
        f"База {DB_FILE} сделана версией бота без поддержки нескольких чатов: "
        "в ней истории всех чатов лежат вперемешку, а message_id уникален глобально. "
        "Удалите или переименуйте файл базы - новая создастся сама."
    )


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


def strip_service_note(text: str) -> str:
    """
    Срезает служебную пометку о пересылке или ответе, если модель продублировала ее.

    :param text: текст ответа модели
    :type text: str
    :return: текст без ведущей служебной пометки
    :rtype: str
    """
    cleaned, replaced = SERVICE_NOTE_PATTERN.subn("", text, count=1)
    return cleaned.lstrip() if replaced else text


def strip_service_prefixes(text: str) -> str:
    """
    Убирает из начала ответа модели служебную разметку контекста.

    :param text: текст ответа модели
    :type text: str
    :return: текст без ведущих служебных пометок
    :rtype: str
    """
    return strip_author_tag(strip_service_note(text))


def shorten(text: str, limit: int) -> str:
    """
    Сжимает текст в одну строку и обрезает до предела.

    Одна строка нужна, чтобы служебная пометка не разъезжалась на несколько строк и
    не путалась с настоящим текстом реплики.

    :param text: исходный текст
    :type text: str
    :param limit: сколько символов оставить
    :type limit: int
    :return: однострочный текст не длиннее предела
    :rtype: str
    """
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[:limit].rstrip() + "…"


def describe_reply_target(target: dict | None) -> str:
    """
    Описывает сообщение, на которое отвечают: чье оно и с чего начиналось.

    :param target: строка таблицы messages в виде словаря или None, если адресата
        не нашлось в базе
    :type target: dict | None
    :return: описание адресата для служебной пометки
    :rtype: str
    """
    if target is None:
        # Отвечать могут и на сообщение старше бота: его в базе нет и уже не будет.
        return "сообщение, которого нет в истории"

    author = (
        "бота" if target.get("is_bot") else f'"{target.get("username") or "unknown"}"'
    )
    snippet = shorten(target.get("content") or "", REPLY_SNIPPET_LIMIT)
    description = f"сообщение {author}"
    # У пересланного адресата автор подписи - тот, кто переслал, а слова в нем чужие.
    forwarded = target.get("forward_origin")
    if forwarded:
        description += f" (переслано {forwarded})"
    if snippet:
        description += f": «{snippet}»"
    media_type = target.get("media_type")
    if media_type:
        description += f" [вложение: {media_type}]"
    return description


def describe_forward_origin(origin: MessageOrigin | None) -> str | None:
    """
    Описывает, откуда переслали сообщение и кто написал его на самом деле.

    :param origin: происхождение пересланного сообщения или None, если ничего не пересылали
    :type origin: MessageOrigin | None
    :return: описание источника для служебной пометки или None, если пересылки не было
    :rtype: str | None
    """
    if origin is None:
        return None

    # Время берем из оригинала: в message.date у пересланного лежит момент пересылки.
    date = str(origin.date)

    if isinstance(origin, MessageOriginUser):
        user = origin.sender_user
        tag = build_author_tag(user.full_name or str(user.id), user.username, date)
        return f'от "{tag}"'

    if isinstance(origin, MessageOriginHiddenUser):
        # Автор закрыл ссылку на свой аккаунт: от него осталось одно имя без ника.
        return f'от скрытого пользователя "{build_author_tag(origin.sender_user_name, None, date)}"'

    if isinstance(origin, (MessageOriginChat, MessageOriginChannel)):
        from_chat = isinstance(origin, MessageOriginChat)
        chat = origin.sender_chat if from_chat else origin.chat
        tag = build_author_tag(
            chat.title or chat.full_name or str(chat.id), chat.username, date
        )
        description = f'из {"чата" if from_chat else "канала"} "{tag}"'
        # В каналах автор поста подписывается отдельно от самого канала.
        if origin.author_signature:
            description += f", подпись автора: {origin.author_signature}"
        return description

    # Telegram может завести новый тип источника: сказать про сам факт пересылки полезнее,
    # чем промолчать и отдать чужие слова за слова переславшего.
    return f"из неизвестного источника (date:{date})"


def build_service_note(msg: dict) -> str | None:
    """
    Собирает служебную пометку перед репликой: откуда она переслана и чему отвечает.

    Цитата идет отдельным куском: она показывает, какую именно часть сообщения выделил
    отвечающий, и приходит даже тогда, когда самого адресата в чате нет (цитата из
    другого чата приезжает без reply_to_message_id).

    :param msg: строка таблицы messages в виде словаря; адресат должен быть уже подложен
        в ключ "reply_target" (см. attach_reply_targets)
    :type msg: dict
    :return: пометка в квадратных скобках или None, если помечать нечего
    :rtype: str | None
    """
    notes = []
    forwarded = msg.get("forward_origin")
    if forwarded:
        notes.append(f"{FORWARD_NOTE_LEAD} {forwarded}")
    if msg.get("reply_to_message_id"):
        target = describe_reply_target(msg.get("reply_target"))
        notes.append(f"{REPLY_NOTE_LEAD} {target}")
    quote = shorten(msg.get("quote_text") or "", QUOTE_SNIPPET_LIMIT)
    if quote:
        notes.append(f"{QUOTE_NOTE_LEAD}: «{quote}»")
    return f"[{'. '.join(notes)}]" if notes else None


def save_message_to_db(  # pylint: disable=too-many-locals
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
    # Фрагмент, который отвечающий выделил в чужом сообщении. Есть и у цитат из других
    # чатов - там это единственный след того, чему отвечали: reply_to_message_id пуст.
    quote_text = message.quote.text if message.quote else None
    # Настоящий автор пересланного: в from_user ниже стоит тот, кто нажал "переслать".
    forward_origin = describe_forward_origin(message.forward_origin)
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
            mime_type, file_id, file_name, timestamp, reply_to_message_id,
            quote_text, forward_origin, is_bot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            quote_text,
            forward_origin,
            is_bot,
        ),
    )
    conn.commit()
    logger.info("Сохранено сообщение %s в БД.", message.message_id)  # lazy logging
    return file_id, mime_type, file_name


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


def attach_reply_targets(conn: sqlite3.Connection, chat_id: int, messages: list):
    """
    Подкладывает к каждой реплике-ответу сообщение, которому она отвечает.

    Ищем по всей истории чата, а не по переданному куску: адресат мог остаться далеко
    позади и уже уйти в пересказ, но пометка о нем все равно нужна. За пределы чата не
    выходим - там лежат чужие разговоры с такими же номерами сообщений.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param messages: сообщения контекста; в отвечающие добавляется ключ "reply_target"
        со строкой адресата или None, если такого сообщения в базе нет
    :type messages: list
    """
    target_ids = {
        msg["reply_to_message_id"] for msg in messages if msg.get("reply_to_message_id")
    }
    if not target_ids:
        return

    cursor = conn.cursor()
    # В строку запроса подставляем только число "?" - сами идентификаторы идут параметрами.
    cursor.execute(
        f"""
        SELECT * FROM messages
        WHERE chat_id = ? AND message_id IN ({",".join("?" * len(target_ids))})
        """,
        (chat_id, *target_ids),
    )
    columns = [description[0] for description in cursor.description]
    targets = {}
    for row in cursor.fetchall():
        target = dict(zip(columns, row))
        targets[target["message_id"]] = target

    for msg in messages:
        reply_to_id = msg.get("reply_to_message_id")
        if reply_to_id:
            msg["reply_target"] = targets.get(reply_to_id)


def get_context(conn: sqlite3.Connection, chat_id: int) -> tuple[str | None, list]:
    """
    Получает контекст чата: пересказ старой части истории и сообщения после нее.

    Пока сжатия не было, пересказ пуст и возвращается вся история чата. Чужие чаты в
    контекст не попадают: у каждого своя история, свой пересказ и свои ключи.

    Args:
        conn (sqlite3.Connection): Соединение с базой данных.
        chat_id (int): Идентификатор чата.

    Returns:
        tuple: (текст пересказа или None, список словарей с информацией о сообщениях;
        у реплик-ответов в ключе "reply_target" лежит сообщение, которому они отвечают).
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM messages WHERE chat_id = ? AND summarized = 0
        ORDER BY timestamp ASC, message_id ASC
    """,
        (chat_id,),
    )
    columns = [description[0] for description in cursor.description]
    messages = [dict(zip(columns, row)) for row in cursor.fetchall()]
    attach_reply_targets(conn, chat_id, messages)
    return get_latest_summary(conn, chat_id), messages


# --- БЛОК УТИЛИТ ДЛЯ МЕДИА ---

# Файлы, выгруженные в Files API. Ключ кэша - пара (ключ Gemini, путь к файлу): выгрузка
# принадлежит проекту того ключа, которым ее делали, и после ротации ссылка на нее
# становится чужой. Поэтому для каждого ключа файл выгружается заново.
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


def check_file_validity(client: genai.Client, api_key: str, media_path: str):
    """
    Проверяет валидность файла

    :param client: клиент ИИ
    :type client: genai.Client
    :param api_key: ключ Gemini, которым файл выгружали
    :type api_key: str
    :param media_path: путь к файлу
    :type media_path: str
    """
    cache_key = (api_key, media_path)
    if cache_key in uploaded_files:
        try:
            remote_file = client.files.get(name=uploaded_files[cache_key].name)
            if remote_file.state.name != "ACTIVE":
                logger.info(
                    "Файл %s в состоянии %s, требуется перевыгрузка",
                    media_path,
                    remote_file.state.name,
                )
                del uploaded_files[cache_key]
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Не удалось проверить статус файла %s, перевыгружаем: %s",
                media_path,
                e,
            )
            del uploaded_files[cache_key]


def upload_file(client: genai.Client, api_key: str, media_path):
    """
    Загружает файл

    :param client: клиент ИИ
    :type client: genai.Client
    :param api_key: ключ Gemini, от имени которого идет выгрузка
    :type api_key: str
    :param media_path: путь к файлу
    :type media_path: str
    """
    uploaded_file = client.files.upload(file=media_path)

    # Цикл ожидания перехода в рабочее состояние
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state.name == "ACTIVE":
        uploaded_files[(api_key, media_path)] = uploaded_file
    else:
        logger.error(
            "Файл %s после загрузки перешел в состояние %s",
            media_path,
            uploaded_file.state.name,
        )


class GeminiRetryError(Exception):
    """Не удалось получить корректный ответ от Gemini за отведенное число попыток."""


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


def handle_api_failure(
    pool: api_keys.KeyPool, key: dict, exc: Exception, attempt: int
) -> Exception | None:
    """
    Разбирает ошибку API: помечает ключ и решает, стоит ли ждать перед повтором.

    :param pool: пул ключей чата
    :type pool: api_keys.KeyPool
    :param key: ключ, на котором упал запрос
    :type key: dict
    :param exc: пойманное исключение
    :type exc: Exception
    :param attempt: номер попытки - уходит в текст неустранимой ошибки
    :type attempt: int
    :return: исключение, если ошибка временная и перед повтором надо выждать паузу,
        или None, если ждать нечего: ключ уже помечен негодным и сменится сам
    :rtype: Exception | None
    :raises GeminiRetryError: если повторять бессмысленно
    """
    kind = api_keys.classify_api_error(exc)
    code = api_keys.get_error_code(exc)

    if kind == api_keys.ERROR_KIND_FATAL:
        logger.error("Неустранимая ошибка Gemini (код %s): %s", code, exc)
        raise GeminiRetryError(
            f"Неустранимая ошибка API на попытке {attempt} из {MAX_RETRIES} "
            f"(код {code}): {exc}"
        ) from exc

    if kind == api_keys.ERROR_KIND_DAILY:
        pool.mark_daily_exhausted(key)
        return None
    if kind == api_keys.ERROR_KIND_KEY:
        pool.mark_broken(key, exc)
        return None

    # Минутный лимит и временные сбои: ключ живой, надо просто подождать.
    return exc


async def generate_with_retries(
    pool: api_keys.KeyPool, model: str, make_contents
) -> str:
    """
    Запрашивает ответ у Gemini, повторяя попытки при сбоях и меняя выдохшиеся ключи.

    Повторяет до MAX_RETRIES раз с экспоненциально растущей паузой. Ошибка ошибке рознь:
    выбранная дневная квота и отвергнутый ключ означают, что надо брать следующий ключ и
    идти дальше без паузы; минутный лимит - что ключ живой и надо просто подождать;
    кривой запрос или несуществующая модель не пройдут никогда, и на них бот сдается.

    Смена ключа тратит попытку. Так цикл не может закружиться на пуле из сотни мертвых
    ключей, а на живом пуле лишние попытки и не понадобятся.

    :param pool: пул ключей чата
    :type pool: api_keys.KeyPool
    :param model: имя модели Gemini
    :type model: str
    :param make_contents: корутина, собирающая содержимое запроса под переданный ключ
    :return: текст ответа модели
    :rtype: str
    :raises GeminiRetryError: если попытки исчерпаны
    :raises api_keys.NoUsableKeys: если в чате не осталось рабочих ключей
    """
    last_reason = "причина неизвестна"
    contents = None
    contents_key_id = None

    for attempt in range(1, MAX_RETRIES + 1):
        key = pool.active()
        if contents is None or contents_key_id != key["id"]:
            # Ссылки на выгруженные файлы принадлежат тому ключу, которым их выгружали,
            # поэтому после смены ключа запрос собирается заново.
            contents = await make_contents(key)
            contents_key_id = key["id"]

        failure = None
        try:
            # Вызов синхронный, уводим его в поток, чтобы не морозить event loop.
            response = await asyncio.to_thread(
                pool.client(key).models.generate_content,
                model=model,
                contents=contents,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            last_reason = f"ошибка API {api_keys.get_error_code(e)}: {e}"
            failure = handle_api_failure(pool, key, e, attempt)
        else:
            pool.note_request(key)
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

        # Ключ уже помечен негодным - ждать нечего, следующий виток возьмет другой.
        if not pool.is_usable(key, ignore_local_limit=True):
            continue

        if attempt < MAX_RETRIES:
            delay = get_backoff_delay(attempt, failure)
            logger.info("Повтор через %.1f с.", delay)
            await asyncio.sleep(delay)

    raise GeminiRetryError(
        f"Не удалось получить ответ за {MAX_RETRIES} попыток. Последняя причина: {last_reason}"
    )


def build_message_parts(client: genai.Client, api_key: str, msg: dict) -> list:
    """
    Превращает одно сообщение из БД в части запроса к модели.

    :param client: клиент ИИ
    :type client: genai.Client
    :param api_key: ключ Gemini, которым работает клиент
    :type api_key: str
    :param msg: строка таблицы messages в виде словаря
    :type msg: dict
    :return: список частей (текст плюс медиа, если оно есть)
    :rtype: list
    """
    parts = []
    content = msg.get("content")
    if msg.get("is_bot"):
        # Свои реплики модель получает без служебных пометок, чтобы не копировать их
        # в новые ответы: роль "model" и так говорит, чьи это слова.
        parts.append(genai.types.Part(text=content or "[Пустой ответ]"))
    else:
        author = msg.get("username") or "unknown"
        text = f"[{author}]: {content}" if content else f"[{author}]"
        note = build_service_note(msg)
        parts.append(genai.types.Part(text=f"{note}\n{text}" if note else text))

    if msg.get("file_id") and msg.get("mime_type"):
        raw_path = get_media_path(
            msg["file_id"], msg["mime_type"], msg.get("file_name")
        )
        media_path = os.path.abspath(raw_path) if raw_path else None
        if media_path and os.path.exists(media_path):
            try:
                file_size = os.path.getsize(media_path)
                if file_size < 20 * 1024 * 1024:
                    # Выгрузка принадлежит ключу, поэтому и кэш ведется по паре с ним.
                    cache_key = (api_key, media_path)
                    # Проверяем, есть ли файл в кэше и валиден ли он
                    check_file_validity(client, api_key, media_path)

                    # Загрузка, если файла нет в кэше (или он был удален выше)
                    if cache_key not in uploaded_files:
                        upload_file(client, api_key, media_path)

                    # Если файл успешно загружен и активен
                    if cache_key in uploaded_files:
                        parts.append(
                            genai.types.Part(
                                file_data=genai.types.FileData(
                                    file_uri=uploaded_files[cache_key].uri,
                                    mime_type=uploaded_files[cache_key].mime_type,
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


def build_history(key: dict, context_messages: list) -> list:
    """
    Готовит историю переписки в виде реплик для модели.

    Рядом с ролью и частями кладем исходную строку БД: по ней сжатие потом определяет,
    на каком сообщении провести границу.

    Медиа по дороге выгружается в Files API, то есть функция ходит в сеть и может
    занять заметное время - зовите ее через asyncio.to_thread.

    :param key: строка ключа Gemini из пула чата
    :type key: dict
    :param context_messages: список сообщений контекста
    :type context_messages: list
    :return: список словарей вида {"role", "parts", "source"}
    :rtype: list
    """
    client = api_keys.client_for_key(key["api_key"])
    history = []
    for msg in context_messages:
        parts = build_message_parts(client, key["api_key"], msg)
        if parts:
            role = "model" if msg.get("is_bot") else "user"
            history.append({"role": role, "parts": parts, "source": msg})
    return history


def build_contents(history: list, summary: str | None) -> list:
    """
    Собирает итоговый запрос: системный промпт, пересказ и история.

    Последняя реплика запроса всегда пользовательская: запрос, который заканчивается
    репликой роли "model", Gemini отклоняет неустранимой ошибкой 400.

    :param history: история переписки от build_history (непустая)
    :type history: list
    :param summary: пересказ сжатой части истории или None
    :type summary: str | None
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


async def count_context_tokens(
    pool: api_keys.KeyPool, key: dict, model: str, contents: list
) -> int | None:
    """
    Считает точный размер запроса токенайзером Gemini.

    :param pool: пул ключей чата
    :type pool: api_keys.KeyPool
    :param key: ключ, которым идем в API
    :type key: dict
    :param model: имя модели Gemini
    :type model: str
    :param contents: подготовленное содержимое запроса
    :type contents: list
    :return: число токенов или None, если посчитать не вышло
    :rtype: int | None
    """
    try:
        # Вызов синхронный, уводим его в поток, чтобы не морозить event loop.
        response = await asyncio.to_thread(
            pool.client(key).models.count_tokens, model=model, contents=contents
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
    pool: api_keys.KeyPool, model: str, messages: list, previous_summary: str | None
) -> str:
    """
    Просит модель пересказать кусок истории одним текстом.

    Предыдущий пересказ идет в запрос вместе с историей, чтобы он не потерялся:
    новый пересказ заменяет его целиком.

    :param pool: пул ключей чата
    :type pool: api_keys.KeyPool
    :param model: имя модели Gemini
    :type model: str
    :param messages: сообщения, которые надо сжать
    :type messages: list
    :param previous_summary: прошлый пересказ или None, если сжимаем впервые
    :type previous_summary: str | None
    :return: текст нового пересказа
    :rtype: str
    :raises GeminiRetryError: если модель так и не ответила
    """

    async def make_contents(key: dict) -> list:
        """Собирает запрос на пересказ под конкретный ключ."""
        entries = await asyncio.to_thread(build_history, key, messages)
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
                        genai.types.PartDict(
                            text=f"{SUMMARY_HEADER}\n{previous_summary}"
                        )
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
        return contents

    logger.info("Сжимаем %d самых старых сообщений в пересказ...", len(messages))
    return (await generate_with_retries(pool, model, make_contents)).strip()


async def compress_context(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    pool: api_keys.KeyPool,
    model: str,
    conn: sqlite3.Connection,
    chat_id: int,
    messages: list,
    summary: str | None,
) -> tuple[list, str | None]:
    """
    Ужимает контекст чата до лимита и возвращает то, что в него уложилось.

    Пока запрос не влезает в MAX_CONTEXT_TOKENS, самая старая часть истории уходит
    модели на пересказ, пересказ попадает в БД и занимает ее место. Один проход не
    всегда доводит до цели (пересказ тоже занимает место), поэтому проходов может
    быть несколько.

    Сжатие - это оптимизация, а не обязательный шаг: если посчитать токены или
    получить пересказ не вышло, отдаем контекст как есть и даем разбираться
    обычному механизму повторов.

    :param pool: пул ключей чата
    :type pool: api_keys.KeyPool
    :param model: имя модели Gemini
    :type model: str
    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param messages: сообщения контекста (непустой список)
    :type messages: list
    :param summary: пересказ сжатой ранее части истории или None
    :type summary: str | None
    :return: (оставшиеся дословно сообщения, актуальный пересказ)
    :rtype: tuple[list, str | None]
    """
    key = pool.active()
    history = await asyncio.to_thread(build_history, key, messages)

    # Дешевая прикидка на входе: обычный чат до лимита не дотягивает, и тратить на него
    # лишний запрос к API незачем. Дальше по кругу идем уже только с точным подсчетом -
    # ошибись прикидка, и сжатие остановилось бы, не дойдя до лимита.
    if estimate_context_tokens(history, summary) < (
        MAX_CONTEXT_TOKENS * TOKEN_CHECK_RATIO
    ):
        return messages, summary

    for _ in range(MAX_COMPRESSION_ROUNDS):
        # Пересказ мог упереться в квоту и сменить ключ: ссылки на выгруженные файлы
        # принадлежат прежнему ключу, поэтому историю приходится пересобрать.
        current_key = pool.active()
        if current_key["id"] != key["id"]:
            key = current_key
            history = await asyncio.to_thread(build_history, key, messages)

        total_tokens = await count_context_tokens(
            pool, key, model, build_contents(history, summary)
        )
        if total_tokens is None:
            return messages, summary
        if total_tokens <= MAX_CONTEXT_TOKENS:
            logger.info("Контекст: %d токенов из %d.", total_tokens, MAX_CONTEXT_TOKENS)
            return messages, summary

        cut = choose_cut_index(history, total_tokens)
        if cut <= 0:
            logger.warning(
                "Контекст (%d токенов) больше лимита %d, но сжимать уже нечего.",
                total_tokens,
                MAX_CONTEXT_TOKENS,
            )
            return messages, summary

        logger.info(
            "Контекст разросся до %d токенов при лимите %d, сжимаем.",
            total_tokens,
            MAX_CONTEXT_TOKENS,
        )
        compressed = [entry["source"] for entry in history[:cut]]
        try:
            summary = await summarize_history(pool, model, compressed, summary)
        except GeminiRetryError as e:
            logger.error("Не удалось сжать контекст, отправляем как есть: %s", e)
            return messages, summary

        save_summary(conn, chat_id, summary, [msg["message_id"] for msg in compressed])
        history = history[cut:]
        messages = [entry["source"] for entry in history]

    logger.warning(
        "Контекст не уложился в лимит за %d проходов.", MAX_COMPRESSION_ROUNDS
    )
    return messages, summary


async def generate_gemini_response(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    pool: api_keys.KeyPool,
    model: str,
    conn: sqlite3.Connection,
    chat_id: int,
    context_messages: list,
    summary: str | None,
):
    """
    Генерирует ответ с использованием модели Google Gemini AI на основе контекста чата.

    Args:
        pool (api_keys.KeyPool): Пул ключей этого чата.
        model (str): Имя модели Gemini, выбранное чатом.
        conn (sqlite3.Connection): Соединение с БД - нужно, чтобы сохранить пересказ.
        chat_id (int): Идентификатор чата.
        context_messages (list): Список сообщений контекста.
        summary (str | None): Пересказ сжатой ранее части истории.

    Returns:
        str: Сгенерированный ответ.
    """
    logger.info(
        "Чат %s: подготовка %d сообщений контекста для модели %s.",
        chat_id,
        len(context_messages),
        model,
    )
    if not context_messages:
        logger.warning("Контекст для Gemini пуст. Отмена запроса.")
        return "Не могу обработать пустой запрос."

    messages, summary = await compress_context(
        pool, model, conn, chat_id, context_messages, summary
    )

    async def make_contents(key: dict) -> list:
        """Собирает запрос под конкретный ключ: медиа выгружается от его имени."""
        history = await asyncio.to_thread(build_history, key, messages)
        return build_contents(history, summary)

    logger.info("Отправка запроса в Gemini...")

    # Генерируем ответ с новым API, повторяя попытки при сбоях
    response_text = await generate_with_retries(pool, model, make_contents)
    return strip_service_prefixes(response_text)


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
    context: ContextTypes.DEFAULT_TYPE, message: Message, transcribe_only: bool
):
    """
    Собирает контекст чата, спрашивает модель и отправляет ответ.

    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    :param message: сообщение, на которое отвечаем
    :type message: Message
    :param transcribe_only: это голосовое без триггера, нужна одна расшифровка
    :type transcribe_only: bool
    """
    db_conn = context.bot_data["db_conn"]
    chat_id = message.chat_id
    summary, context_messages = get_context(db_conn, chat_id)

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

    pool = api_keys.KeyPool(db_conn, chat_id, KEY_RPD_LIMIT)
    model = chat_settings.get_model(db_conn, chat_id, MODEL)

    placeholder = await send_placeholder(message)
    err = False

    try:
        response_text = await generate_gemini_response(
            pool, model, db_conn, chat_id, context_messages, summary
        )
    except api_keys.NoUsableKeys as e:
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

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст обработчика.
    """
    message = update.effective_message
    if not message or message.chat.type not in ("group", "supergroup", "private"):
        return

    db_conn = context.bot_data["db_conn"]
    trigger = TRIGGER_WORD.lower()
    triggered_by_text = (
        message.text and message.text.lower().startswith(trigger)
    ) or (message.caption and message.caption.lower().startswith(trigger))

    file_id, mime_type, file_name = save_message_to_db(db_conn, message, is_bot=False)
    if file_id:
        file_path = get_media_path(file_id, mime_type, file_name)
        if file_path:
            await download_media_file(context.application, file_id, file_path)

    if not (triggered_by_text or message.voice):
        return

    # Внутри чата ответы идут по очереди, а разные чаты друг друга не ждут: запрос к
    # модели с повторами растягивается на минуты, и одному чату незачем держать все
    # остальные. Сообщения при этом сохраняются сразу, до очереди, - история не отстает.
    async with chat_lock(context, message.chat_id):
        await answer_chat(
            context, message, bool(message.voice) and not triggered_by_text
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
    Инициализирует подключения к базе данных и API, настраивает обработчики сообщений.
    """
    if not BOT_TOKEN:
        raise ValueError("Пожалуйста, проверьте файл конфигурации: BOT_TOKEN не указан.")

    db_connection = init_db()

    # Ключ из конфига доступен всем чатам сразу; свои чат добавляет командой /addkey.
    api_keys.sync_shared_key(db_connection, GEMINI_API_KEY)
    if not GEMINI_API_KEY:
        logger.info(
            "Общий ключ в config.cfg не задан - чаты работают только на своих ключах."
        )

    # Чаты обслуживаются параллельно: ответ с повторами занимает минуты, и один чат не
    # должен становиться очередью для всех остальных. Порядок внутри чата держит замок.
    application = (
        Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    )

    application.bot_data["db_conn"] = db_connection
    application.bot_data["default_model"] = MODEL
    application.bot_data["key_rpd_limit"] = KEY_RPD_LIMIT

    for name, handler in COMMAND_HANDLERS.items():
        application.add_handler(CommandHandler(name, handler))

    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
    )

    logger.info(
        "Модель по умолчанию: %s. Потолок запросов на ключ в сутки: %s.",
        MODEL,
        KEY_RPD_LIMIT or "не задан",
    )
    logger.info("Бот запускается...")
    application.run_polling()

    db_connection.close()
    logger.info("Соединение с БД закрыто.")


if __name__ == "__main__":
    main()
