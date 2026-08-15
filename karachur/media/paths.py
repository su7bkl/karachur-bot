"""
Первая половина пути вложения: где оно ляжет на диске и как туда попадет.

Телеграм не присылает файл вместе с сообщением - он присылает file_id, по которому файл
надо забрать отдельным запросом, и mime, которому не всегда можно верить. Здесь ровно эти
две заботы: придумать файлу имя в каталоге медиа и скачать его туда.

Имя берется от самого вложения, если у него есть собственное имя из одних ascii-символов:
так в каталоге видно, что за файл там лежит, а не одни идентификаторы. Имя при этом
чистится от всего, кроме букв, цифр и немногих безобидных знаков, - в частности из него
уходит слэш, поэтому вложение не может увести путь за пределы каталога медиа. Все
остальные случаи - имени нет или оно не ascii - собирают имя из file_id и расширения,
выведенного по mime: file_id уникален, и гадать с ним не о чем.

Дальше файл уходит в karachur.media.normalize, которая может его перекодировать и тем
самым сменить и расширение, и путь. Поэтому итоговый путь пишется в базу
(messages.set_media_path), а не вычисляется этими функциями заново при каждой сборке
контекста: get_media_path остается только для сообщений, сохраненных до появления той
колонки.
"""

import logging
import os

from telegram.ext import Application

logger = logging.getLogger(__name__)


def get_extension_from_mime(mime: str | None) -> str:
    """
    Подбирает расширение файла по его mime.

    Точного соответствия не ищем: mime от Телеграма бывает и приблизительным, и
    отсутствующим, а расширение нужно лишь для того, чтобы файл на диске выглядел
    прилично и открывался внешними программами.

    :param mime: mime-тип файла или None, если Телеграм его не прислал
    :type mime: str | None
    :return: расширение без точки
    :rtype: str
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
    media_dir: str, file_id: str, mime_type: str | None, original_name: str | None
) -> str | None:
    """
    Формирует путь к файлу для сохранения медиа.

    :param media_dir: каталог, в котором бот держит скачанные файлы
    :type media_dir: str
    :param file_id: идентификатор файла в Telegram
    :type file_id: str
    :param mime_type: MIME-тип файла
    :type mime_type: str | None
    :param original_name: оригинальное имя файла
    :type original_name: str | None
    :return: путь к файлу или None, если файл не может быть сохранен
    :rtype: str | None
    """
    if original_name and original_name.isascii():
        safe_name = "".join(
            c for c in original_name if c.isalnum() or c in (" ", ".", "_", "-")
        ).strip()
        return os.path.join(media_dir, safe_name)
    if file_id:
        ext = get_extension_from_mime(mime_type)
        return os.path.join(media_dir, f"{file_id}.{ext}")
    return None


async def download_media_file(application: Application, file_id: str, file_path: str):
    """
    Скачивает вложение из Telegram в заранее выбранный путь.

    Уже лежащий на месте файл повторно не качается: один и тот же file_id может прийти
    в чат несколько раз, а Telegram отдает по нему то же самое.

    Неудача скачивания не поднимается наружу: бот должен ответить на сообщение и без
    вложения - в контекст оно просто не попадет.

    :param application: приложение Telegram, через которое идет запрос файла
    :type application: Application
    :param file_id: идентификатор файла в Telegram
    :type file_id: str
    :param file_path: путь, по которому файл надо сохранить
    :type file_path: str
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
