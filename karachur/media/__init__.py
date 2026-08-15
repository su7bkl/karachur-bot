"""
Приведение скачанного медиа к тому, что Gemini заведомо понимает.

Телеграм отдает файлы такими, какими их собрал отправитель, и модель на этом спотыкается
двумя разными способами. Первый - битый контейнер: формат вроде бы знакомый, а конкретный
файл рассогласован, и generateContent отвечает "400 Request contains an invalid argument"
без единого намека, что не так (история этого случая - в ffmpeg.py). Второй - формат,
которого модель не знает вовсе: docx, json, zip. Первое лечится пересборкой через ffmpeg,
второе - конвертацией в PDF, сменой mime или отказом отдавать файл модели.

Отсюда и разделение пакета:
    policy    - решает, что делать с парой (mime, имя файла): пять действий и ни одного
                обращения к самому файлу, кроме подглядывания в первые 8 КБ
    ffmpeg    - пересобирает видео, аудио и картинки
    documents - переводит офисные документы в PDF через LibreOffice

Наружу торчит одна функция - normalize: bot.py зовет ее сразу после скачивания файла и
запоминает, что она вернула. Возврат mime=None - это "модели не отдавать": bot.py грузит
файл только при непустом mime, и пропуск архива выражается именно так.

Все неудачи здесь нефатальны. Нет ffmpeg, нет LibreOffice, конвертация не задалась - файл
остается прежним: нормализация улучшает шансы, но не имеет права ломать работу бота.
"""

import logging
import os

from karachur.media import documents, policy
from karachur.media.ffmpeg import encode, remove_quietly

logger = logging.getLogger(__name__)

PDF_MIME = "application/pdf"


def normalize(path: str, mime_type: str | None) -> tuple[str, str | None]:
    """
    Приводит скачанный файл к тому, что модель сможет прочитать.

    Функция синхронная и ходит во внешние программы, то есть блокирует надолго - зовите
    ее через asyncio.to_thread.

    :param path: путь к скачанному файлу
    :type path: str
    :param mime_type: mime, с которым файл пришел из Telegram
    :type mime_type: str | None
    :return: (путь, mime) после приведения; mime=None означает, что файл модели не годится
        и отдавать его не надо
    :rtype: tuple[str, str | None]
    """
    if not os.path.exists(path):
        return path, mime_type

    action, mime = policy.decide(mime_type, path)

    if action is policy.Action.ENCODE:
        return encode(path, mime)

    if action is policy.Action.PDF:
        return _convert_to_pdf(path, mime_type)

    if action is policy.Action.RETAG:
        # Файл не трогаем: это уже текст, ему не хватало только честного mime.
        logger.info("Файл %s (%s) уходит модели как %s.", path, mime_type, mime)
        return path, mime

    if action is policy.Action.SKIP:
        logger.info("Файл %s (%s) модели не годится, отдаем только текст.", path, mime_type)
        return path, None

    return path, mime


def _convert_to_pdf(path: str, mime_type: str | None) -> tuple[str, str | None]:
    """
    Переводит документ в PDF и прибирает за собой исходник.

    :param path: путь к скачанному документу
    :type path: str
    :param mime_type: mime, с которым файл пришел из Telegram
    :type mime_type: str | None
    :return: (путь к pdf, application/pdf) либо исходная пара, если конвертация не вышла
    :rtype: tuple[str, str | None]
    """
    pdf = documents.to_pdf(path)
    if pdf is None:
        # Без LibreOffice ведем себя как без ffmpeg: файл уходит модели как есть. Шансов
        # у него немного, но это ровно то, что было до появления конвертации, - хуже не
        # стало, а решать за пользователя, что его документ "не считается", незачем.
        return path, mime_type

    # Исходник больше не нужен: в контекст пойдет pdf.
    remove_quietly(path)
    logger.info("Документ переведен в PDF: %s (%s) -> %s", path, mime_type, pdf)
    return pdf, PDF_MIME
