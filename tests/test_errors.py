"""
Тесты разбора ошибок Gemini: кто виноват - ключ, файл или вообще никто.

Граница проходит внутри одного кода 403, и в этом вся сложность. Одним и тем же
PERMISSION_DENIED Gemini отвечает и на отозванный ключ, и на ссылку на выгрузку,
которой больше нет. Разница видна только в тексте, а цена ошибки несимметрична:
похороненный ключ оживает лишь руками через /delkey и /addkey, тогда как лишний повтор
стоит пары минут. Поэтому здесь закреплена вся таблица целиком - и та ее половина, где
ключ виноват, и та, где нет.

Ошибки записаны так, как их присылает настоящая Gemini: разбор смотрит на текст, и
проверять его на выдуманных формулировках бессмысленно.
"""

import pytest

from karachur.gemini import errors

# Ключ и правда мертв: сам он не воскреснет, и повторять запрос им незачем.
DEAD_KEY_ERRORS = [
    pytest.param(
        400,
        "INVALID_ARGUMENT. API key not valid. Please pass a valid API key. "
        "reason: API_KEY_INVALID",
        id="невалидный ключ",
    ),
    pytest.param(
        403,
        "PERMISSION_DENIED. API key expired. Please renew the API key. "
        "reason: API_KEY_INVALID",
        id="ключ отозван",
    ),
    pytest.param(
        403,
        "PERMISSION_DENIED. Consumer 'projects/123456' has been suspended. "
        "reason: CONSUMER_SUSPENDED",
        id="проект заблокирован",
    ),
    pytest.param(
        403,
        "PERMISSION_DENIED. Requests from this IP address are blocked. "
        "reason: API_KEY_IP_ADDRESS_BLOCKED",
        id="ограничение по IP",
    ),
    pytest.param(
        403,
        "PERMISSION_DENIED. Requests from referer are blocked. "
        "reason: API_KEY_HTTP_REFERRER_BLOCKED",
        id="ограничение по referer",
    ),
    pytest.param(
        403,
        "PERMISSION_DENIED. Generative Language API has not been used in project "
        "123456 before or it is disabled. reason: SERVICE_DISABLED",
        id="API не включен в проекте",
    ),
    pytest.param(401, "UNAUTHENTICATED. Request had invalid credentials.", id="401"),
]

# Беда в ссылке на выгрузку, а не в ключе: лечится перевыгрузкой файла.
STALE_FILE_ERRORS = [
    pytest.param(
        403,
        "PERMISSION_DENIED. You do not have permission to access the File "
        "nk9d2v0zzz1q or it may not exist.",
        id="нет доступа к File",
    ),
    pytest.param(
        404,
        "NOT_FOUND. File files/nk9d2v0zzz1q does not exist.",
        id="File не существует",
    ),
    pytest.param(
        400,
        "INVALID_ARGUMENT. The File files/nk9d2v0zzz1q is not in an ACTIVE state "
        "and usage is not allowed.",
        id="File не в состоянии ACTIVE",
    ),
]


@pytest.mark.parametrize("code, message", DEAD_KEY_ERRORS)
def test_dead_key_is_recognized(api_error, code, message):
    """Настоящий отказ по ключу по-прежнему опознается как беда ключа."""
    assert errors.classify_api_error(api_error(code, message)) == errors.ERROR_KIND_KEY


@pytest.mark.parametrize("code, message", STALE_FILE_ERRORS)
def test_stale_file_is_not_blamed_on_the_key(api_error, code, message):
    """Ошибка про пропавший файл - это ошибка про файл, а не про ключ."""
    assert errors.classify_api_error(api_error(code, message)) == errors.ERROR_KIND_FILE


def test_unrecognized_permission_denied_spares_the_key(api_error):
    """
    Неопознанный 403 ключа не хоронит.

    Именно эта строка таблицы дороже всех: пока сюда попадал ERROR_KIND_KEY, любая
    незнакомая формулировка PERMISSION_DENIED выкашивала пул чата ключ за ключом.
    """
    unknown = api_error(403, "PERMISSION_DENIED. Причина, которой мы не знаем.")

    assert errors.classify_api_error(unknown) == errors.ERROR_KIND_TRANSIENT


def test_file_check_wins_over_the_key_branch(api_error):
    """
    Файл разбирается раньше ключа, даже когда в тексте есть и то и другое.

    Проверка нужна из-за порядка веток: стоит проверке на файл уехать ниже разбора по
    коду - и она перестанет срабатывать вовсе, молча, без единого падения теста.
    """
    mixed = api_error(
        403,
        "PERMISSION_DENIED. You do not have permission to access the File files/xyz. "
        "API key: AIza...",
    )

    assert errors.classify_api_error(mixed) == errors.ERROR_KIND_FILE


def test_file_word_in_lowercase_is_not_a_files_api_error(api_error):
    """
    Строчное "file" - не про Files API, и выгрузки тут ни при чем.

    Ресурс Files API Gemini пишет с большой буквы, а строчное слово встречается в
    ошибках совсем про другое - например, про размер тела запроса.
    """
    oversized = api_error(400, "INVALID_ARGUMENT. Request payload file size exceeds")

    assert errors.classify_api_error(oversized) == errors.ERROR_KIND_FATAL


def test_server_error_about_a_file_stays_transient(api_error):
    """Пятисотка с упоминанием файла - все равно временный сбой, а не мертвая ссылка."""
    broken = api_error(500, "INTERNAL. File processing failed")

    assert errors.classify_api_error(broken) == errors.ERROR_KIND_TRANSIENT
