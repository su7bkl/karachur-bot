"""
Политика форматов: что делать с файлом, прежде чем отдать его Gemini.

Телеграм присылает файлы вперемешку - фото, войсы, видео-стикеры, документы,
экспортированные из чужого чата, а иногда и откровенный мусор без внятного mime. Часть
таких файлов Gemini принимает как есть, часть - только после ffmpeg (см.
karachur.media.ffmpeg: там же объяснено, почему даже формально поддерживаемые видео и
аудио всё равно пересобираются - битым бывает не формат, а конкретный файл), часть -
только в виде PDF, а часть отдавать модели вообще не стоит: она либо не поймёт, либо
потратит контекст впустую.

Этот модуль ничего не скачивает и не конвертирует - только решает, каким путём файл
пойдёт дальше, по паре (mime от Телеграма, имя файла). Разбираться самому приходится
потому, что Телеграм часто врёт про mime: для незнакомых расширений он подставляет
application/octet-stream, а то и вовсе ничего не присылает. В этом случае решение
принимается по расширению файла, а если и оно ничего не говорит - по первым байтам
содержимого.
"""

import codecs
import enum
import os


class Action(enum.Enum):
    """Что сделать с файлом перед тем, как отдать его модели."""

    KEEP = "keep"       # формат из белого списка Gemini, файл не трогаем
    ENCODE = "encode"   # гнать через ffmpeg
    PDF = "pdf"          # конвертировать в PDF через LibreOffice
    RETAG = "retag"      # это текст под неподдерживаемым mime: сменить mime, файл не трогать
    SKIP = "skip"        # двоичный мусор, модели не отдаём


# Полный белый список Gemini дословно из документации. Используется как справка и в
# тестах - для принятия решений ниже он не годится целиком: часть этих mime всё равно
# уходит не в KEEP, а в ENCODE (см. комментарии у EXACT_ENCODE_MIMES и KEEP_MIMES).
WHITELIST = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif",
    "video/mp4", "video/mpeg", "video/mov", "video/avi", "video/x-flv",
    "video/mpg", "video/webm", "video/wmv", "video/3gpp",
    "audio/wav", "audio/mp3", "audio/aiff", "audio/aac", "audio/ogg", "audio/flac",
    "application/pdf",
    "text/plain", "text/markdown", "text/html", "text/xml", "text/csv",
})

# Эти четыре формата в белом списке есть, но ffmpeg всё равно переводит их в png или mp4
# (см. CONVERSIONS в ffmpeg.py), поэтому точное совпадение должно сработать раньше
# группового правила "все image/* оставляем как есть". image/gif в белом списке
# отсутствует намеренно - гифки обязаны уходить в mp4, а не сдаваться модели картинкой.
EXACT_ENCODE_MIMES = frozenset({"image/gif", "image/webp", "image/heic", "image/heif"})

# Остаток белого списка, который действительно остаётся как есть. video/* и audio/*
# сюда не входят: они перехватываются группой раньше (см. _classify_mime) - модели
# отдаётся не формат, а конкретный файл, и полагаться на то, что он не побит, нельзя.
KEEP_MIMES = frozenset({
    "image/png", "image/jpeg",
    "application/pdf",
    "text/plain", "text/markdown", "text/html", "text/xml", "text/csv",
})

# Офисные форматы Gemini не понимает вовсе - только PDF. Точные mime для тех, что не
# описываются общим префиксом вендора.
DOCUMENT_MIMES = frozenset({
    "application/msword",
    "application/rtf",
    "text/rtf",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/epub+zip",
})
# У Office Open XML и OpenDocument mime всегда начинается с одного из этих префиксов,
# а дальше идёт конкретный тип (wordprocessingml.document, spreadsheetml.sheet и т.д.) -
# перечислять все хвосты смысла нет, они все одинаково уходят в PDF.
DOCUMENT_MIME_PREFIXES = (
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
)

# Текстовые форматы, которые прячутся под mime из мира application/*. Смысл файла от
# смены mime не теряется - он просто перестаёт быть "непонятным" для модели.
TEXT_APPLICATION_MIMES = frozenset({
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/x-sh",
    "application/toml",
    "application/x-toml",
    "application/x-python",
    "application/sql",
})

# Архивы и двоичные форматы, которые модели отдавать бессмысленно. Внимание: docx, xlsx,
# pptx, odt, epub тоже технически zip-контейнеры, но они перехватываются проверкой
# документов раньше и сюда не попадают - см. порядок в _classify_mime.
ARCHIVE_MIMES = frozenset({
    "application/zip",
    "application/x-rar-compressed",
    "application/vnd.rar",
    "application/x-7z-compressed",
    "application/x-tar",
    "application/gzip",
    "application/x-gzip",
    "application/x-bzip2",
    "application/x-xz",
    "application/x-msdownload",
    "application/x-msi",
    "application/vnd.android.package-archive",
    "application/x-iso9660-image",
    "application/x-apple-diskimage",
    "application/vnd.debian.binary-package",
    "application/x-rpm",
    "application/x-executable",
    "application/x-sharedlib",
})

# Телеграм присылает application/octet-stream (или вовсе ничего) для файлов, чьё
# расширение ему незнакомо. По расширению можно понять куда больше, чем по такому mime -
# таблица одна на все пять действий, потому что расширения между категориями не
# пересекаются и путать порядок проверки не с чем.
EXTENSION_RULES: dict[str, tuple[Action, str | None]] = {
    # видео и gif - тоже в mp4
    ".mp4": (Action.ENCODE, "video/mp4"),
    ".mpeg": (Action.ENCODE, "video/mpeg"),
    ".mpg": (Action.ENCODE, "video/mpg"),
    ".mov": (Action.ENCODE, "video/mov"),
    ".avi": (Action.ENCODE, "video/avi"),
    ".flv": (Action.ENCODE, "video/x-flv"),
    ".webm": (Action.ENCODE, "video/webm"),
    ".wmv": (Action.ENCODE, "video/wmv"),
    ".3gp": (Action.ENCODE, "video/3gpp"),
    ".3gpp": (Action.ENCODE, "video/3gpp"),
    ".gif": (Action.ENCODE, "image/gif"),
    # аудио
    ".wav": (Action.ENCODE, "audio/wav"),
    ".mp3": (Action.ENCODE, "audio/mp3"),
    ".aiff": (Action.ENCODE, "audio/aiff"),
    ".aif": (Action.ENCODE, "audio/aiff"),
    ".aac": (Action.ENCODE, "audio/aac"),
    ".ogg": (Action.ENCODE, "audio/ogg"),
    ".oga": (Action.ENCODE, "audio/ogg"),
    ".opus": (Action.ENCODE, "audio/ogg"),
    ".flac": (Action.ENCODE, "audio/flac"),
    ".m4a": (Action.ENCODE, "audio/aac"),
    # изображения, которые ffmpeg переводит в png
    ".webp": (Action.ENCODE, "image/webp"),
    ".heic": (Action.ENCODE, "image/heic"),
    ".heif": (Action.ENCODE, "image/heif"),
    # белый список без изменений
    ".png": (Action.KEEP, "image/png"),
    ".jpg": (Action.KEEP, "image/jpeg"),
    ".jpeg": (Action.KEEP, "image/jpeg"),
    ".pdf": (Action.KEEP, "application/pdf"),
    ".txt": (Action.KEEP, "text/plain"),
    ".md": (Action.KEEP, "text/markdown"),
    ".markdown": (Action.KEEP, "text/markdown"),
    ".html": (Action.KEEP, "text/html"),
    ".htm": (Action.KEEP, "text/html"),
    ".xml": (Action.KEEP, "text/xml"),
    ".csv": (Action.KEEP, "text/csv"),
    # офисные документы - в PDF через LibreOffice
    ".doc": (Action.PDF, "application/msword"),
    ".docx": (
        Action.PDF, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".odt": (Action.PDF, "application/vnd.oasis.opendocument.text"),
    ".rtf": (Action.PDF, "application/rtf"),
    ".xls": (Action.PDF, "application/vnd.ms-excel"),
    ".xlsx": (
        Action.PDF, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    ".ods": (Action.PDF, "application/vnd.oasis.opendocument.spreadsheet"),
    ".ppt": (Action.PDF, "application/vnd.ms-powerpoint"),
    ".pptx": (
        Action.PDF,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ".odp": (Action.PDF, "application/vnd.oasis.opendocument.presentation"),
    ".epub": (Action.PDF, "application/epub+zip"),
    # исходники и конфиги - текст под чужим (или отсутствующим) mime
    ".json": (Action.RETAG, "text/plain"),
    ".yaml": (Action.RETAG, "text/plain"),
    ".yml": (Action.RETAG, "text/plain"),
    ".ini": (Action.RETAG, "text/plain"),
    ".cfg": (Action.RETAG, "text/plain"),
    ".conf": (Action.RETAG, "text/plain"),
    ".toml": (Action.RETAG, "text/plain"),
    ".log": (Action.RETAG, "text/plain"),
    ".py": (Action.RETAG, "text/plain"),
    ".js": (Action.RETAG, "text/plain"),
    ".ts": (Action.RETAG, "text/plain"),
    ".jsx": (Action.RETAG, "text/plain"),
    ".tsx": (Action.RETAG, "text/plain"),
    ".sh": (Action.RETAG, "text/plain"),
    ".bash": (Action.RETAG, "text/plain"),
    ".sql": (Action.RETAG, "text/plain"),
    ".css": (Action.RETAG, "text/plain"),
    ".scss": (Action.RETAG, "text/plain"),
    ".c": (Action.RETAG, "text/plain"),
    ".h": (Action.RETAG, "text/plain"),
    ".hpp": (Action.RETAG, "text/plain"),
    ".cpp": (Action.RETAG, "text/plain"),
    ".cc": (Action.RETAG, "text/plain"),
    ".java": (Action.RETAG, "text/plain"),
    ".go": (Action.RETAG, "text/plain"),
    ".rs": (Action.RETAG, "text/plain"),
    ".rb": (Action.RETAG, "text/plain"),
    ".php": (Action.RETAG, "text/plain"),
    ".pl": (Action.RETAG, "text/plain"),
    ".lua": (Action.RETAG, "text/plain"),
    ".kt": (Action.RETAG, "text/plain"),
    ".swift": (Action.RETAG, "text/plain"),
    ".srt": (Action.RETAG, "text/plain"),
    ".vtt": (Action.RETAG, "text/plain"),
    ".diff": (Action.RETAG, "text/plain"),
    ".patch": (Action.RETAG, "text/plain"),
    ".env": (Action.RETAG, "text/plain"),
    # архивы и двоичный мусор
    ".zip": (Action.SKIP, None),
    ".rar": (Action.SKIP, None),
    ".7z": (Action.SKIP, None),
    ".tar": (Action.SKIP, None),
    ".gz": (Action.SKIP, None),
    ".tgz": (Action.SKIP, None),
    ".bz2": (Action.SKIP, None),
    ".xz": (Action.SKIP, None),
    ".exe": (Action.SKIP, None),
    ".msi": (Action.SKIP, None),
    ".apk": (Action.SKIP, None),
    ".iso": (Action.SKIP, None),
    ".dmg": (Action.SKIP, None),
    ".deb": (Action.SKIP, None),
    ".rpm": (Action.SKIP, None),
    ".so": (Action.SKIP, None),
    ".dll": (Action.SKIP, None),
    # .bin тут намеренно нет: это расширение придумывает сам бот (get_media_path в
    # bot.py дает его файлу, про который Телеграм не прислал вообще никакого mime).
    # Оно означает "неизвестно что", а не "двоичное", - и такой файл должен дойти до
    # проверки по содержимому, иначе присланный текст молча пропадет из контекста.
}

# Столько байт хватает, чтобы отличить текст от двоичного мусора, не читая файл целиком -
# и не настолько мало, чтобы короткая, но осмысленная шапка текстового файла срезалась.
TEXT_SNIFF_SIZE = 8192


def _normalize_mime(mime_type: str | None) -> str:
    """
    Приводит mime к сравнимому виду: без регистра и без довеска вида "; charset=utf-8".

    :param mime_type: сырой mime от Телеграма
    :type mime_type: str | None
    :return: нормализованный mime или пустая строка, если он не пришёл
    :rtype: str
    """
    if not mime_type:
        return ""
    return mime_type.lower().split(";")[0].strip()


def _classify_mime(mime: str) -> Action | None:
    """
    Определяет действие по нормализованному mime - лестница в точности из правил 2-7.

    :param mime: нормализованный, непустой mime
    :type mime: str
    :return: действие или None, если mime не попал ни в одну категорию
    :rtype: Action | None
    """
    if mime in EXACT_ENCODE_MIMES or mime.startswith(("video/", "audio/")):
        return Action.ENCODE
    if mime in KEEP_MIMES:
        return Action.KEEP
    if mime in DOCUMENT_MIMES or mime.startswith(DOCUMENT_MIME_PREFIXES):
        return Action.PDF
    if mime.startswith("text/") or mime in TEXT_APPLICATION_MIMES:
        return Action.RETAG
    if mime in ARCHIVE_MIMES:
        return Action.SKIP
    return None


def _by_mime(mime: str) -> tuple[Action, str | None] | None:
    """
    Решает по mime, каким действующим mime это действие сопровождается.

    :param mime: нормализованный, непустой mime
    :type mime: str
    :return: (действие, действующий mime) или None, если mime ничего не сказал
    :rtype: tuple[Action, str | None] | None
    """
    action = _classify_mime(mime)
    if action is None:
        return None
    if action is Action.RETAG:
        return action, "text/plain"
    if action is Action.SKIP:
        return action, None
    return action, mime


def _by_extension(path: str) -> tuple[Action, str | None] | None:
    """
    Решает по расширению файла - выручает, когда mime от Телеграма ничего не говорит.

    :param path: путь к файлу (расширение берётся из имени)
    :type path: str
    :return: (действие, действующий mime) или None, если расширение не опознано
    :rtype: tuple[Action, str | None] | None
    """
    extension = os.path.splitext(path)[1].lower()
    return EXTENSION_RULES.get(extension) if extension else None


def looks_like_text(path: str) -> bool:
    """
    Смотрит в первые байты файла и решает, похож ли он на текст.

    Последний довод, когда и mime, и расширение ничего не говорят. Нулевой байт в
    начале файла у настоящего текста не встречается - это надёжный признак двоичного
    формата. Для UTF-8 используется потоковый декодер с final=False: 8-килобайтная
    граница может разрезать многобайтовый символ пополам, и обычный decode() на таком
    обрубке ошибочно принял бы честный текст за мусор.

    :param path: путь к файлу
    :type path: str
    :return: True, если похоже на текст
    :rtype: bool
    """
    try:
        with open(path, "rb") as source:
            chunk = source.read(TEXT_SNIFF_SIZE)
    except OSError:
        return False

    if b"\x00" in chunk:
        return False

    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(chunk, final=False)
    except UnicodeDecodeError:
        return False
    return True


def decide(mime_type: str | None, path: str) -> tuple[Action, str | None]:
    """
    Решает, что делать с файлом, прежде чем отдавать его модели.

    Сначала пробуем mime - он приходит от Телеграма и, если осмыслен, самый надёжный
    источник. Если mime не пришёл, пуст или Телеграм честно расписался в незнании
    (application/octet-stream и подобное) - как и если незнакомый mime не попал ни в
    одну категорию - решаем по расширению имени файла, а если и оно ничего не говорит -
    по содержимому. Сам файл при этом не трогается и не конвертируется, только читается
    для распознавания в самом крайнем случае.

    :param mime_type: mime, присланный Телеграмом
    :type mime_type: str | None
    :param path: путь к скачанному файлу
    :type path: str
    :return: (действие, действующий mime для дальнейшей работы)
    :rtype: tuple[Action, str | None]
    """
    mime = _normalize_mime(mime_type)
    if mime:
        by_mime = _by_mime(mime)
        if by_mime is not None:
            return by_mime

    by_extension = _by_extension(path)
    if by_extension is not None:
        return by_extension

    if looks_like_text(path):
        return Action.RETAG, "text/plain"
    return Action.SKIP, None
