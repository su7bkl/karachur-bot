"""
Приведение скачанного медиа к форматам, которые Gemini заведомо понимает.

Телеграм отдает файлы в том виде, в каком их собрал отправитель, и среди них попадаются
контейнеры, на которых модель спотыкается. Реальный случай: видео-стикер, у которого в
контейнере записано 33 мс, а в видеодорожке - 6 секунд. Файл при этом читается любым
плеером, загружается в Files API и переходит в ACTIVE, а generateContent отвечает на него
"400 Request contains an invalid argument" - без единого намека, что именно не так.

Разобрать такое заранее нечем: битым бывает не формат, а конкретный файл. Поэтому все
медиа проходит через ffmpeg сразу после скачивания - он читает исходник своим разбором и
пишет заново, уже с согласованными заголовками. Заодно приводим пестрый набор форматов к
короткому списку тех, что Gemini принимает наверняка.

Что во что переводится:
    видео и gif  -> mp4  (H.264 + AAC)
    аудио и голос -> ogg/opus (голосовые - переливкой, без пересжатия)
    webp, heic   -> png
    jpeg, png    -> остаются как есть
    pdf, текст и прочее - не трогаем, там перекодировать нечего

Если ffmpeg недоступен или не справился, файл остается прежним: перекодирование улучшает
шансы, но не должно ломать работу бота.
"""

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
# Даже длинное видео на 20 МБ укладывается в минуту, а зависший ffmpeg держал бы чат.
CONVERSION_TIMEOUT = 180

# H.264 не умеет нечетные стороны, а Telegram присылает и 489x512. Округляем вниз.
EVEN_SIDES = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

# Видео пересобираем целиком: именно здесь чинятся рассогласованные заголовки.
VIDEO_ARGS = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "28",
    "-pix_fmt", "yuv420p",
    "-vf", EVEN_SIDES,
    "-c:a", "aac",
    "-b:a", "96k",
    "-movflags", "+faststart",
]
# Аудио сводим к opus в ogg. Голосовые Telegram и так присылает в этом виде, поэтому их
# просто переливаем в новый контейнер: заголовки пересобираются, а дорожка остается байт
# в байт прежней - для расшифровки речи это заметно лучше повторного сжатия. Все
# остальное (mp3, wav, aac из видео) пережимаем в тот же opus.
AUDIO_COPY = ["-vn", "-c:a", "copy", "-f", "ogg"]
AUDIO_OPUS = ["-vn", "-c:a", "libopus", "-b:a", "64k", "-f", "ogg"]
# Из анимированного webp берем первый кадр: смысла в нем ровно на одну картинку.
IMAGE_ARGS = ["-frames:v", "1"]

def audio_codec(path: str) -> str | None:
    """
    Определяет кодек первой аудиодорожки файла.

    :param path: путь к файлу
    :type path: str
    :return: имя кодека или None, если определить не вышло
    :rtype: str | None
    """
    command = [
        FFPROBE, "-loglevel", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path,
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, timeout=CONVERSION_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Не удалось определить кодек %s: %s", path, e)
        return None
    return done.stdout.decode("utf-8", "replace").strip() or None


def audio_attempts(path: str) -> list[list[str]]:
    """
    Выбирает, как поступить с аудио: перелить как есть или пережать в opus.

    :param path: путь к файлу
    :type path: str
    :return: способы перекодирования по порядку
    :rtype: list[list[str]]
    """
    if audio_codec(path) == "opus":
        # Уже opus - пережимать нечего, но если переливка не выйдет, пережмем.
        return [AUDIO_COPY, AUDIO_OPUS]
    return [AUDIO_OPUS]


# Во что переводить каждый тип. Ключ - начало mime, значение - (расширение, mime,
# способы). Способы задаются списком или функцией от пути, если выбор зависит от файла.
CONVERSIONS = {
    "video/": ("mp4", "video/mp4", [VIDEO_ARGS]),
    "image/gif": ("mp4", "video/mp4", [VIDEO_ARGS]),
    "audio/": ("ogg", "audio/ogg", audio_attempts),
    "image/webp": ("png", "image/png", [IMAGE_ARGS]),
    "image/heic": ("png", "image/png", [IMAGE_ARGS]),
    "image/heif": ("png", "image/png", [IMAGE_ARGS]),
}


def conversion_for(mime_type: str | None) -> tuple | None:
    """
    Подбирает правило перекодирования по mime-типу.

    :param mime_type: mime исходного файла
    :type mime_type: str | None
    :return: (расширение, новый mime, способы перекодирования) или None, если трогать
        не надо. Способы - список или функция от пути к файлу
    :rtype: tuple | None
    """
    if not mime_type:
        return None

    mime = mime_type.lower().split(";")[0].strip()
    # Точное совпадение важнее группового: image/webp разбирается не как все image/.
    if mime in CONVERSIONS:
        return CONVERSIONS[mime]
    for prefix, rule in CONVERSIONS.items():
        if prefix.endswith("/") and mime.startswith(prefix):
            return rule
    return None


def run_ffmpeg(source: str, target: str, args: list[str]) -> bool:
    """
    Зовет ffmpeg. Вынесено отдельной функцией, чтобы тесты могли ее подменить.

    :param source: путь к исходному файлу
    :type source: str
    :param target: путь, куда писать результат
    :type target: str
    :param args: аргументы кодирования
    :type args: list[str]
    :return: True, если ffmpeg отработал без ошибки
    :rtype: bool
    """
    command = [FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", source, *args, target]
    try:
        done = subprocess.run(
            command, capture_output=True, timeout=CONVERSION_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Не удалось запустить ffmpeg для %s: %s", source, e)
        return False

    if done.returncode != 0:
        error = done.stderr.decode("utf-8", "replace").strip().splitlines()
        logger.warning(
            "ffmpeg не смог перекодировать %s: %s",
            source,
            error[-1] if error else f"код {done.returncode}",
        )
        return False
    return True


def normalize(path: str, mime_type: str | None) -> tuple[str, str | None]:
    """
    Перекодирует файл в формат, понятный модели.

    Функция синхронная и ходит в ffmpeg, то есть блокирует надолго - зовите ее через
    asyncio.to_thread.

    :param path: путь к скачанному файлу
    :type path: str
    :param mime_type: mime, с которым файл пришел из Telegram
    :type mime_type: str | None
    :return: (путь, mime) - новые после перекодирования или прежние, если она не нужна
        или не удалась
    :rtype: tuple[str, str | None]
    """
    rule = conversion_for(mime_type)
    if rule is None or not os.path.exists(path):
        return path, mime_type

    extension, new_mime, ways = rule
    if shutil.which(FFMPEG) is None:
        logger.warning("ffmpeg не найден, %s уходит модели как есть.", path)
        return path, mime_type

    attempts = ways(path) if callable(ways) else ways

    base = os.path.splitext(path)[0]
    target = f"{base}.{extension}"
    # Пишем во временный файл: исходник еще понадобится, если ffmpeg не справится, а
    # цель может совпадать с ним по имени (mp4 в mp4 мы тоже пересобираем).
    staging = f"{base}.converting.{extension}"

    if not any(run_ffmpeg(path, staging, args) for args in attempts):
        _remove(staging)
        return path, mime_type

    try:
        os.replace(staging, target)
    except OSError as e:
        logger.warning("Не удалось положить перекодированный файл %s: %s", target, e)
        _remove(staging)
        return path, mime_type

    if target != path:
        # Исходник больше не нужен: в контекст пойдет перекодированный файл.
        _remove(path)

    logger.info("Медиа перекодировано: %s (%s) -> %s (%s)", path, mime_type, target, new_mime)
    return target, new_mime


def _remove(path: str):
    """
    Убирает файл, не поднимая шум, если его уже нет.

    :param path: путь к файлу
    :type path: str
    """
    try:
        os.remove(path)
    except OSError:
        pass
