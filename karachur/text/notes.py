"""
Служебные пометки, которые бот подмешивает к репликам перед отправкой в Gemini.

Модель видит историю чата плоским текстом, поэтому все, что в интерфейсе Telegram
передается структурой сообщения (кто ответил, на что ответил, что процитировал, что
переслал, чем является вложение), приходится проговаривать текстом прямо в реплике.
Модуль собирает такие пометки и умеет срезать их обратно, если модель случайно
продублировала пометку в начале своего ответа.
"""

import re

from telegram import (
    MessageOrigin,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

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
# Перед моделью все вложения выглядят одинаково: после перекодирования и стикер, и кружок,
# и гифка приезжают одним и тем же mp4. Пометка возвращает то, что при этом теряется, -
# чем это было в чате.
MEDIA_NOTE_LEAD = "Вложение"
MEDIA_LABELS = {
    "photo": "фото",
    "sticker": "стикер",
    "animated_sticker": "анимированный стикер",
    "video_sticker": "видео-стикер",
    "animation": "гифка",
    "video": "видео",
    "video_note": "видеосообщение кружком",
    "voice": "голосовое сообщение",
    "audio": "аудиозапись",
    "document": "файл",
}
# Сколько символов сообщения-адресата показываем: пометка должна давать его опознать,
# а не пересказывать целиком - иначе популярное сообщение размножится по всему контексту.
REPLY_SNIPPET_LIMIT = 300
# Цитату Telegram режет на своей стороне (около 1024 символов), порог тут - страховка.
QUOTE_SNIPPET_LIMIT = 1024
# Страховка на случай, если модель начнет ответ с копии пометки.
SERVICE_NOTE_PATTERN = re.compile(
    rf"^\s*\[(?:{FORWARD_NOTE_LEAD}|{REPLY_NOTE_LEAD}|{QUOTE_NOTE_LEAD}"
    rf"|{MEDIA_NOTE_LEAD})[^\n]*\]\s*"
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


def describe_sticker(sticker) -> tuple[str, str, str]:
    """
    Разбирает стикер: какого он вида и с каким mime его сохранять.

    Вид нужен только для служебной пометки - сам файл отправляется как раньше.

    :param sticker: стикер из сообщения Telegram
    :return: (вид вложения, file_id, mime)
    :rtype: tuple[str, str, str]
    """
    if sticker.is_animated:
        return "animated_sticker", sticker.file_id, "video/webm"
    if sticker.is_video:
        return "video_sticker", sticker.file_id, "video/webm"
    return "sticker", sticker.file_id, "image/webp"


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
    label = MEDIA_LABELS.get(msg.get("media_type") or "")
    if label:
        notes.append(f"{MEDIA_NOTE_LEAD}: {label}")
    return f"[{'. '.join(notes)}]" if notes else None
