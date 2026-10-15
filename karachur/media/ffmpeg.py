"""
Движок перекодирования: все, что умеет ffmpeg.

Телеграм отдает файлы в том виде, в каком их собрал отправитель, и среди них попадаются
контейнеры, на которых модель спотыкается. Реальный случай: видео-стикер, у которого в
контейнере записано 33 мс, а в видеодорожке - 6 секунд. Файл при этом читается любым
плеером, загружается в Files API и переходит в ACTIVE, а generateContent отвечает на него
"400 Request contains an invalid argument" - без единого намека, что именно не так.

Разобрать такое заранее нечем: битым бывает не формат, а конкретный файл. Поэтому все
медиа проходит через ffmpeg сразу после скачивания - он читает исходник своим разбором и
пишет заново, уже с согласованными заголовками. Заодно приводим пестрый набор форматов к
короткому списку тех, что Gemini принимает наверняка.

Короткие видео дополняем последним кадром до двух секунд: валидный пятикадровый
стикер длительностью 0,167 секунды Gemini отвергала тем же 400. Это защитная мера,
проверенная на таком файле, а не документированный минимум API. Новые файлы проходят
ее при encode, старые — при ensure_video_duration перед выгрузкой. Для старых файлов
сохраняем отдельную копию, чтобы не менять исходник и не использовать его старую
ссылку из кэша Files API.

Что во что переводится:
    видео и gif  -> mp4  (H.264 + AAC)
    аудио и голос -> ogg/opus (голосовые - переливкой, без пересжатия)
    webp, heic   -> png

Кого сюда вообще звать, решает policy: этот модуль занят только кодированием и о
документах, тексте и архивах ничего не знает.

Если ffmpeg недоступен или не справился, файл остается прежним: перекодирование улучшает
шансы, но не должно ломать работу бота.
"""

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
# Даже длинное видео на 20 МБ укладывается в минуту, а зависший ffmpeg держал бы чат.
CONVERSION_TIMEOUT = 180

# H.264 не умеет нечетные стороны, а Telegram присылает и 489x512. Округляем вниз.
EVEN_SIDES = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
# Обход отказа Gemini на валидных видео-стикерах из нескольких кадров.
# Это выбранная нами защитная длительность, а не документированный лимит API.
MIN_VIDEO_DURATION = 2.0

# Видео пересобираем целиком: именно здесь чинятся рассогласованные заголовки.
VIDEO_ARGS = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "28",
    "-pix_fmt", "yuv420p",
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


def video_duration(path: str) -> float | None:
    """Читает длительность дорожки и контейнера, сохраняя более длинное значение."""
    try:
        done = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration:format=duration", "-of", "json", path],
            capture_output=True, timeout=CONVERSION_TIMEOUT, check=False,
        )
        if done.returncode:
            return None
        data = json.loads(done.stdout)
        stream = next(iter(data.get("streams", [])), {})
        durations = []
        for value in (stream.get("duration"), data.get("format", {}).get("duration")):
            if value in (None, "N/A"):
                continue
            duration = float(value)
            if math.isfinite(duration) and duration > 0:
                durations.append(duration)
        # Не обрезаем длинную аудиодорожку у файла с короткой видеодорожкой.
        return max(durations) if durations else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def _video_args(duration: float | None) -> list[str]:
    """Общие параметры видео для скачанных файлов и старой истории."""
    filters = [EVEN_SIDES]
    duration_args = []
    if duration is not None and duration < MIN_VIDEO_DURATION:
        filters.append(
            f"tpad=stop_mode=clone:stop_duration={MIN_VIDEO_DURATION}"
        )
        duration_args = ["-t", str(MIN_VIDEO_DURATION)]
    return [*VIDEO_ARGS, "-vf", ",".join(filters), *duration_args]


def video_attempts(path: str) -> list[list[str]]:
    """Подбирает параметры видео, продлевая короткие клипы последним кадром."""
    duration = video_duration(path)
    if duration is not None and duration < MIN_VIDEO_DURATION:
        logger.info("Продлеваем короткое видео %s: %.3fs -> %.1fs",
                    path, duration, MIN_VIDEO_DURATION)
    return [_video_args(duration)]


def ensure_video_duration(path: str) -> str:
    """Возвращает пригодную по длительности копию старого видео либо исходник."""
    if shutil.which(FFMPEG) is None:
        return path
    target = f"{os.path.splitext(path)[0]}.gemini-min2.mp4"
    if (os.path.exists(target)
            and os.path.getmtime(target) >= os.path.getmtime(path)):
        return target
    duration = video_duration(path)
    if duration is None or duration >= MIN_VIDEO_DURATION:
        return path
    if not _convert(path, target, [_video_args(duration)]):
        return path
    logger.info("Короткое видео истории подготовлено: %s -> %s", path, target)
    return target


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
    "video/": ("mp4", "video/mp4", video_attempts),
    "image/gif": ("mp4", "video/mp4", video_attempts),
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


def encode(path: str, mime_type: str | None) -> tuple[str, str | None]:
    """
    Перекодирует файл в формат, понятный модели.

    Функция синхронная и ходит в ffmpeg, то есть блокирует надолго - зовите ее через
    asyncio.to_thread.

    :param path: путь к скачанному файлу
    :type path: str
    :param mime_type: mime, по которому подбирается правило перекодирования
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
    if not _convert(path, target, attempts):
        return path, mime_type

    if target != path:
        # Исходник больше не нужен: в контекст пойдет перекодированный файл.
        remove_quietly(path)

    logger.info("Медиа перекодировано: %s (%s) -> %s (%s)", path, mime_type, target, new_mime)
    return target, new_mime


def _convert(source: str, target: str, attempts: list[list[str]]) -> bool:
    """Публикует результат только после успешного ffmpeg, сохраняя исходник при сбое."""
    staging = None
    try:
        # У параллельных чатов могут быть общие вложения. Каждый вызов пишет в свой
        # временный файл рядом с целью, чтобы os.replace оставался атомарным.
        with tempfile.NamedTemporaryFile(
            prefix=".converting.", suffix=os.path.splitext(target)[1],
            dir=os.path.dirname(os.path.abspath(target)), delete=False,
        ) as temporary:
            staging = temporary.name
        if not any(run_ffmpeg(source, staging, args) for args in attempts):
            return False
        os.replace(staging, target)
        return True
    except OSError as exc:
        logger.warning("Не удалось сохранить перекодированный файл %s: %s", target, exc)
        return False
    finally:
        if staging is not None:
            remove_quietly(staging)


def remove_quietly(path: str):
    """
    Убирает файл, не поднимая шум, если его уже нет.

    Лежит на виду, а не под подчеркиванием, потому что убирать исходник приходится и
    после ffmpeg, и после конвертации документа в PDF: два одинаковых хелпера в соседних
    модулях - тот же код, только дважды.

    :param path: путь к файлу
    :type path: str
    """
    try:
        os.remove(path)
    except OSError:
        pass
