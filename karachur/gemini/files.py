"""
Files API: выгрузка медиа на сторону Gemini и учет уже выгруженного.

Байтами файл в запрос не попадает - в contents уходит ссылка на него, а сам файл заранее
кладется в Files API. Выгрузка не мгновенная: пока Google разбирает файл, тот висит в
состоянии PROCESSING, и ссылка на него бесполезна. Поэтому upload_file ждет ACTIVE прямо
в теле функции, обычным sleep, - и звать его, как и все отсюда, надо через
asyncio.to_thread, иначе встанет весь бот.

Выгруженное живет на стороне Google само по себе и переживает не один запрос, так что
второй раз тот же файл не грузим. Но вечным оно не бывает - выгрузка истекает, - поэтому
перед каждой отправкой у нее спрашивают состояние, а не считают, что раз когда-то
выгрузили, значит, все еще на месте. Смена ключа сюда не относится: она видна прямо в
ключе кэша, и файл просто выгружается заново от имени нового ключа.
"""

import logging
import time

from google import genai

logger = logging.getLogger(__name__)

# Файлы, выгруженные в Files API. Ключ кэша - пара (ключ Gemini, путь к файлу): выгрузка
# принадлежит проекту того ключа, которым ее делали, и после ротации ссылка на нее
# становится чужой. Поэтому для каждого ключа файл выгружается заново.
uploaded_files = {}


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
