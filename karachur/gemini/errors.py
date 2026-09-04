"""
Разбор ошибок Gemini API: что именно случилось с запросом.

На выбранную квоту, на слишком частые запросы и на отвергнутый ключ SDK отдает похожие
исключения, а отвечать на них надо по-разному: одну ошибку достаточно переждать тем же
ключом, из-за другой надо уходить на следующий, третью повторять бессмысленно вовсе.
Здесь только чтение ошибки и ни одного решения о ключе - решения принимают
karachur.gemini.pool и цикл повторов в karachur.gemini.retries.

Читать приходится код и текст: отдельных полей "какая квота кончилась" у ошибки нет, а
сам код в разных версиях SDK лежит то в атрибуте объекта, то в начале сообщения.

Отдельная забота модуля - не приписать ключу чужую вину. За кодом 403 у Gemini стоит
не одна ситуация, а несколько разных, и ключ виноват не во всех: та же ссылка на
протухшую выгрузку в Files API отвечает ровно тем же PERMISSION_DENIED, что и отозванный
ключ. Поэтому 403 разбирается по тексту, а не по коду, и неопознанный 403 ключом не
считается (см. классификацию ниже).
"""

import re

# Дневная квота: ключ выбыл до полуночи, надо брать следующий.
ERROR_KIND_DAILY = "daily"
# Минутный лимит: ключ живой, просто частим - ждем и повторяем тем же ключом.
ERROR_KIND_RATE = "rate"
# Ключ отвергнут: больше он не заработает, помечаем и переключаемся.
ERROR_KIND_KEY = "key"
# Беда не в ключе, а в ссылке на файл из Files API: выгрузка протухла или удалена.
# Ключ при этом совершенно здоров - лечится перевыгрузкой файла, а не сменой ключа.
ERROR_KIND_FILE = "file"
# Беда не в ключе, а в запросе или модели - повторять бессмысленно.
ERROR_KIND_FATAL = "fatal"
# Все остальное (500, обрывы связи, таймауты) - повторяем с паузой.
ERROR_KIND_TRANSIENT = "transient"

# В деталях 429 Gemini называет нарушенную квоту: "GenerateRequestsPerDayPerProjectPerModel".
DAILY_QUOTA_PATTERN = re.compile(r"per[-_ ]?day", re.IGNORECASE)

# Ссылка на выгрузку, которой больше нет, узнается по слову File с большой буквы: так
# Gemini называет ресурс Files API ("access the File xxx", "File xxx does not exist"),
# и той же формы бывает имя ресурса - "files/abc123". Регистр тут значащий, поэтому
# IGNORECASE не ставим: строчное "file" встречается в ошибках совсем про другое
# ("request payload file size"), и по нему мы приняли бы за протухшую выгрузку что
# угодно.
FILE_ERROR_PATTERN = re.compile(r"\bFile\b|\bfiles/[\w-]+")
# Про несуществующий файл Gemini отвечает и 403 (нет доступа), и 404 (нет самого файла),
# и 400 (файл не в состоянии ACTIVE). Ограничиваем разбор этими кодами: 500 с упоминанием
# файла - все равно временный сбой, и повторять его надо как временный сбой.
FILE_ERROR_CODES = frozenset({400, 403, 404})

# Прямые признаки того, что ключ и правда мертв:
#   - API_KEY_INVALID, "API key not valid", "API key expired" - ключ отозван или удален;
#   - API_KEY_IP_ADDRESS_BLOCKED, API_KEY_HTTP_REFERRER_BLOCKED - ключ ограничен по IP
#     или referer, и из-под бота он работать не будет;
#   - CONSUMER_SUSPENDED - проект заблокирован;
#   - SERVICE_DISABLED - в проекте не включен Generative Language API.
# Последний случай теоретически обратим: API включают в консоли одной галочкой. Но сам
# бот об этом не узнает и пробовать заново не станет, а broken_reason виден в /keys -
# человек прочтет внятную причину и добавит ключ заново. Гонять такой ключ по повторам
# было бы хуже: попытки тратятся впустую, а сигнала человеку нет.
DEAD_KEY_PATTERN = re.compile(
    r"api[-_ ]?key"  # API_KEY_INVALID, API_KEY_IP_ADDRESS_BLOCKED, "API key not valid"
    r"|consumer[-_ ]?suspended"  # CONSUMER_SUSPENDED
    r"|service[-_ ]?disabled"  # SERVICE_DISABLED
    r"|has not been used in project"  # человеческий текст того же SERVICE_DISABLED
    r"|project[-_ ]?(?:is[-_ ]?)?suspended",  # человеческий текст CONSUMER_SUSPENDED
    re.IGNORECASE,
)
# 401 - отказ аутентификации, тут разночтений нет: ключ не приняли как ключ.
KEY_ERROR_CODES = frozenset({401})
# Коды, за которыми ключ бывает виноват, но только если про это сказано прямо. 400 попал
# сюда из-за "API key not valid. Please pass a valid API key." - она приезжает именно
# четырехсотым.
DEAD_KEY_CODES = frozenset({400, 403})
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


def _classify_access_error(code: int | None, text: str) -> str | None:
    """
    Разбирает отказ в доступе: за 401 и 403 стоит не одна беда, а несколько разных.

    Порядок веток - часть смысла. Проверка на файл идет первой: ошибка про протухшую
    выгрузку приходит тем же 403 (а бывает, и 400, и 404), и после веток по коду до нее
    бы просто не дошли - ключ поехал бы в mark_broken за чужую вину.

    :param code: HTTP-код ошибки
    :type code: int | None
    :param text: текст ошибки вместе с деталями
    :type text: str
    :return: константа ERROR_KIND_*, если ошибка про доступ, иначе None
    :rtype: str | None
    """
    if code in FILE_ERROR_CODES and FILE_ERROR_PATTERN.search(text):
        return ERROR_KIND_FILE

    if code in KEY_ERROR_CODES:
        return ERROR_KIND_KEY
    if code in DEAD_KEY_CODES and DEAD_KEY_PATTERN.search(text):
        return ERROR_KIND_KEY

    if code == 403:
        # Неопознанный 403 - НЕ повод хоронить ключ, и это не упрощение, а расчет: цена
        # ошибки здесь несимметрична. Похоронить рабочий ключ - значит записать ему
        # broken_reason в базу; сам он оттуда не воскреснет, нужен человек с /delkey и
        # /addkey. Зря повторить запрос - значит потерять пару минут и написать в чат,
        # что не вышло. Поэтому все, чего мы в 403 не узнали, толкуем в пользу ключа.
        # Не "упрощайте" обратно в ERROR_KIND_KEY: именно так пул и выкашивался целиком -
        # одна мертвая ссылка на файл убивала ключ за ключом.
        return ERROR_KIND_TRANSIENT

    return None


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

    access = _classify_access_error(code, text)
    if access is not None:
        return access

    if code in FATAL_ERROR_CODES:
        return ERROR_KIND_FATAL
    return ERROR_KIND_TRANSIENT
