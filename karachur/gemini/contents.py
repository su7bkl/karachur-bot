"""
Сборка запроса к модели: из строк базы - в contents для generateContent.

Три уровня, каждый со своей заботой. build_message_parts берет одно сообщение и делает
из него части реплики: текст с пометкой, кто это написал, и, если было вложение, ссылку
на выгруженный файл - либо, если файл выгружать нельзя, текстовую пометку вместо него
(почему выгружать можно не всё - в _build_media_part). build_history проходит этим по
всему контексту чата и раскладывает реплики по ролям "user" и "model". build_contents
ставит перед историей системный промпт и пересказ сжатой части и отдает то, что уже
можно слать в API.

Сборка привязана к ключу, а не к чату: ссылки на выгруженные файлы принадлежат проекту
того ключа, которым их выгружали (см. karachur.gemini.files), поэтому после ротации
историю надо пересобирать заново - отсюда ключ в аргументах build_history.

Хождение в сеть спрятано именно здесь: build_history через build_message_parts выгружает
медиа в Files API и может провозиться заметное время, так что зовут ее через
asyncio.to_thread.
"""

import logging
import os

import httpx
from google import genai

from karachur.gemini import files

# Модуль зовется key_pool, а не pool: имя pool по всему коду занято самим пулом чата
# (аргументы обработчиков, поле сессии), и модуль под тем же именем ими бы перекрывался.
from karachur.gemini import pool as key_pool
from karachur.media import paths, policy
from karachur.text import notes

logger = logging.getLogger(__name__)

# Заголовок, под которым сжатая история уходит в контекст следующих запросов.
SUMMARY_HEADER = "[Сжатый пересказ более ранней части чата]"

# Потолок Files API. Файл крупнее туда просто не уедет, и проверять это дешевле, чем
# получать отказ на выгрузке.
MAX_UPLOAD_SIZE = 20 * 1024 * 1024

# Чужие беды, из-за которых вложение не удается приложить: диск, Files API и транспорт.
# httpx тут не случайный гость - это транспорт самого google-genai, и обрывы связи
# прилетают наружу именно его исключениями, мимо genai.errors.
MEDIA_PART_FAILURES = (OSError, genai.errors.APIError, httpx.HTTPError)


def _build_media_part(
    client: genai.Client, api_key: str, media_path: str, mime_type: str
) -> genai.types.Part:
    """
    Готовит часть запроса с вложением: ссылку на выгруженный файл либо пометку о пропуске.

    Перед выгрузкой стоят два барьера, и оба заканчиваются текстовой пометкой вместо
    файла - модель должна узнать, что вложение было, но его не показали.

    Первый барьер - размер: в Files API помещается 20 МБ.

    Второй - формат, и он намеренно дублирует karachur.media.policy. Политика уже решала
    судьбу этого файла при скачивании, но её решение могло не исполниться: без
    LibreOffice docx остаётся docx, без ffmpeg битое видео остаётся битым. Такой файл
    Gemini отвергает ошибкой 400, а 400 разбирается как неустранимая (см.
    karachur.gemini.errors): ни повтора, ни смены ключа не будет. Файл при этом остаётся
    в истории чата, и следующий запрос упрётся в него снова - выйти нельзя даже сжатием,
    потому что пересказ собирается по тому же контексту и падает там же. Один такой файл
    глушит чат до ручной правки базы, поэтому дешевле не выгружать его вовсе.

    :param client: клиент ИИ
    :type client: genai.Client
    :param api_key: ключ Gemini, которым работает клиент
    :type api_key: str
    :param media_path: путь к файлу на диске (файл существует)
    :type media_path: str
    :param mime_type: mime, с которым файл ушёл бы модели
    :type mime_type: str
    :return: часть запроса - ссылка на файл или текстовая пометка
    :rtype: genai.types.Part
    """
    file_size = os.path.getsize(media_path)
    if file_size >= MAX_UPLOAD_SIZE:
        logger.warning(
            "Файл %s слишком большой (%.2f МБ), пропускаем",
            media_path,
            file_size / 1024 / 1024,
        )
        return genai.types.Part(text="[Файл слишком большой для обработки - пропущено]")

    if not policy.is_supported(mime_type):
        logger.warning(
            "Файл %s остался в формате %s, который модель не читает, - не выгружаем",
            media_path,
            mime_type,
        )
        return genai.types.Part(
            text=f"[Файл формата {mime_type} модель не читает - пропущено]"
        )

    # Проверяем, есть ли файл в кэше и валиден ли он. Выгрузка принадлежит ключу,
    # поэтому и кэш ведется по паре с ним - отсюда api_key в аргументах.
    files.check_file_validity(client, api_key, media_path)

    # Загрузка, если файла нет в кэше (или он был удален выше)
    if files.cached_file(api_key, media_path) is None:
        files.upload_file(client, api_key, media_path)

    # Если файл успешно загружен и активен
    uploaded = files.cached_file(api_key, media_path)
    if uploaded is None:
        return genai.types.Part(text="[Ошибка обработки файла - не удалось активировать]")

    return genai.types.Part(
        file_data=genai.types.FileData(
            file_uri=uploaded.uri, mime_type=uploaded.mime_type
        )
    )


def build_message_parts(
    client: genai.Client, api_key: str, msg: dict, media_dir: str = ""
) -> list:
    """
    Превращает одно сообщение из БД в части запроса к модели.

    :param client: клиент ИИ
    :type client: genai.Client
    :param api_key: ключ Gemini, которым работает клиент
    :type api_key: str
    :param msg: строка таблицы messages в виде словаря
    :type msg: dict
    :param media_dir: каталог медиа; нужен только сообщениям, сохраненным до появления
        колонки media_path - у них путь к файлу приходится вычислять по mime заново
    :type media_dir: str
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
        note = notes.build_service_note(msg)
        parts.append(genai.types.Part(text=f"{note}\n{text}" if note else text))

    if msg.get("file_id") and msg.get("mime_type"):
        # Путь перекодированного файла лежит в базе: после ffmpeg у него другое
        # расширение, и по mime его уже не вычислить. У сообщений, сохраненных до
        # перекодирования, колонка пуста - для них путь считается по-старому.
        raw_path = msg.get("media_path") or paths.get_media_path(
            media_dir, msg["file_id"], msg["mime_type"], msg.get("file_name")
        )
        media_path = os.path.abspath(raw_path) if raw_path else None
        if media_path and os.path.exists(media_path):
            try:
                parts.append(
                    _build_media_part(client, api_key, media_path, msg["mime_type"])
                )
            except MEDIA_PART_FAILURES as e:
                # Ловим ровно то, что прилетает извне: файл на диске исчез или не
                # читается (OSError), Files API отказал (APIError), связь оборвалась
                # (httpx). На все это ответ один - обойтись без вложения, потому что
                # ронять из-за одной картинки весь ответ чату жалко.
                #
                # Широкого except Exception тут больше нет намеренно. Он глотал и наши
                # собственные KeyError с TypeError: ошибка в сборке части молча
                # превращалась в "вложения не было", и найти ее было негде. Пусть такие
                # падают громко.
                logger.error(
                    "Ошибка при работе с медиафайлом %s: %s",
                    media_path,
                    e,
                )
    return parts


def build_history(key: dict, context_messages: list, media_dir: str) -> list:
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
    :param media_dir: каталог медиа, откуда берутся файлы старых сообщений
    :type media_dir: str
    :return: список словарей вида {"role", "parts", "source"}
    :rtype: list
    """
    client = key_pool.client_for_key(key["api_key"])
    history = []
    for msg in context_messages:
        parts = build_message_parts(client, key["api_key"], msg, media_dir)
        if parts:
            role = "model" if msg.get("is_bot") else "user"
            history.append({"role": role, "parts": parts, "source": msg})
    return history


def build_contents(history: list, summary: str | None, system_prompt: str) -> list:
    """
    Собирает итоговый запрос: системный промпт, пересказ и история.

    Последняя реплика запроса всегда пользовательская: запрос, который заканчивается
    репликой роли "model", Gemini отклоняет неустранимой ошибкой 400.

    :param history: история переписки от build_history (непустая)
    :type history: list
    :param summary: пересказ сжатой части истории или None
    :type summary: str | None
    :param system_prompt: системный промпт, он же первая реплика запроса
    :type system_prompt: str
    :return: содержимое запроса к модели
    :rtype: list
    """
    contents = [
        genai.types.ContentDict(
            role="user", parts=[genai.types.PartDict(text=system_prompt)]
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
