"""
Разбор ошибок Gemini API: что именно случилось с запросом.

На выбранную квоту, на слишком частые запросы и на отвергнутый ключ SDK отдает похожие
исключения, а отвечать на них надо по-разному: одну ошибку достаточно переждать тем же
ключом, из-за другой надо уходить на следующий, третью повторять бессмысленно вовсе.
Здесь только чтение ошибки и ни одного решения о ключе - решения принимают
karachur.gemini.pool и цикл повторов в karachur.gemini.retries.

Читать приходится код и текст: отдельных полей "какая квота кончилась" у ошибки нет, а
сам код в разных версиях SDK лежит то в атрибуте объекта, то в начале сообщения.
"""

import re

# Дневная квота: ключ выбыл до полуночи, надо брать следующий.
ERROR_KIND_DAILY = "daily"
# Минутный лимит: ключ живой, просто частим - ждем и повторяем тем же ключом.
ERROR_KIND_RATE = "rate"
# Ключ отвергнут: больше он не заработает, помечаем и переключаемся.
ERROR_KIND_KEY = "key"
# Беда не в ключе, а в запросе или модели - повторять бессмысленно.
ERROR_KIND_FATAL = "fatal"
# Все остальное (500, обрывы связи, таймауты) - повторяем с паузой.
ERROR_KIND_TRANSIENT = "transient"

# В деталях 429 Gemini называет нарушенную квоту: "GenerateRequestsPerDayPerProjectPerModel".
DAILY_QUOTA_PATTERN = re.compile(r"per[-_ ]?day", re.IGNORECASE)
# "API key not valid. Please pass a valid API key." приезжает кодом 400, но это беда ключа.
BAD_KEY_PATTERN = re.compile(r"api[-_ ]?key", re.IGNORECASE)
KEY_ERROR_CODES = frozenset({401, 403})
FATAL_ERROR_CODES = frozenset({400, 404})


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


def classify_api_error(exc: Exception) -> str:
    """
    Решает, что случилось с запросом и что делать с ключом.

    :param exc: пойманное исключение
    :type exc: Exception
    :return: одна из констант ERROR_KIND_*
    :rtype: str
    """
    code = get_error_code(exc)
    text = str(exc)

    if code == 429:
        # Дневную квоту от минутной отличаем по названию квоты в деталях ошибки: на
        # минутной ключ менять не надо, достаточно подождать.
        if DAILY_QUOTA_PATTERN.search(text):
            return ERROR_KIND_DAILY
        return ERROR_KIND_RATE
    if code in KEY_ERROR_CODES:
        return ERROR_KIND_KEY
    if code == 400 and BAD_KEY_PATTERN.search(text):
        return ERROR_KIND_KEY
    if code in FATAL_ERROR_CODES:
        return ERROR_KIND_FATAL
    return ERROR_KIND_TRANSIENT
