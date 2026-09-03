"""
Тесты кэша выгрузок в Files API: когда бот верит записи, а когда переспрашивает.

Выгрузка живет на стороне Google 48 часов, и после этого ссылка на нее не просто
бесполезна - она опасна: Gemini отвечает на нее 403, а бот раньше читал всякий 403 как
отказ по ключу и хоронил собственные ключи один за другим. Поэтому кэш теперь помнит
срок, сам выбрасывает просроченное и ходит к API только по делу.

Главное, что здесь проверяется, - именно "по делу": свежая запись не должна стоить ни
одного сетевого вызова. Доказывается это счетчиком обращений в подделке Files API
(gemini.file_gets), потому что иначе разницу между "спросили тридцать раз" и "не
спросили ни разу" в поведении бота не увидеть.

Часы тесты не подменяют: сроки задаются смещением от текущего момента, как их и отдает
настоящий Files API.
"""

import os
import time

import pytest

from conftest import KEY_ONE, KEY_TWO, FakeFile
from karachur.gemini import contents, files

MEDIA_PATH = "/media/фото.png"
OTHER_PATH = "/media/второе.png"

# Заведомо свежий срок: до перепроверки (RECHECK_WINDOW) еще далеко.
FRESH = 10 * 60 * 60
# Срок на исходе: запас EXPIRY_MARGIN еще не съеден, но окно перепроверки уже началось.
CLOSE = 30 * 60


def remember(gemini, api_key=KEY_ONE, media_path=MEDIA_PATH, expires_in=FRESH, **kwargs):
    """
    Кладет в кэш выгрузку с заданным сроком, как будто ее сделали раньше.

    :param gemini: подделка Gemini - в ней же файл считается существующим
    :param api_key: ключ, которым файл выгружали
    :param media_path: путь к файлу на диске
    :param expires_in: через сколько секунд Files API удалит выгрузку (можно отрицательно)
    :param kwargs: остальные поля FakeFile
    :return: положенная в кэш запись
    :rtype: files.Upload
    """
    remote = FakeFile(name=f"files/{os.path.basename(media_path)}", **kwargs)
    gemini.publish(remote)
    entry = files.Upload(file=remote, expires_at=time.time() + expires_in)
    files.uploaded_files[(api_key, media_path)] = entry
    return entry


def check(gemini, api_key=KEY_ONE, media_path=MEDIA_PATH):
    """Прогоняет проверку кэша так, как ее зовет сборка запроса."""
    files.check_file_validity(gemini.client_for_key(api_key), api_key, media_path)


def test_fresh_upload_costs_no_network_calls(gemini, uploads_cache):
    """Пока до срока далеко, у Files API ничего не спрашивают."""
    remember(gemini)

    check(gemini)

    assert gemini.file_gets == []
    assert (KEY_ONE, MEDIA_PATH) in uploads_cache


def test_expired_upload_is_dropped_without_asking(gemini, uploads_cache):
    """Просроченную запись выбрасывают молча: спрашивать про нее нечего."""
    remember(gemini, expires_in=-60 * 60)

    check(gemini)

    assert gemini.file_gets == []
    assert not uploads_cache


def test_upload_close_to_expiry_is_verified(gemini, uploads_cache):
    """У самого срока запись все-таки сверяют с API - и продлевают по его ответу."""
    entry = remember(gemini, expires_in=CLOSE)
    # Настоящий срок оказался позже нашего: именно ради этого случая и ходят в сеть.
    entry.file.expiration_time = FakeFile(expires_in=FRESH).expiration_time

    check(gemini)

    assert gemini.file_gets == [entry.file.name]
    assert uploads_cache[(KEY_ONE, MEDIA_PATH)].expires_at == pytest.approx(
        time.time() + FRESH, abs=60
    )


def test_upload_gone_from_files_api_is_forgotten(gemini, uploads_cache):
    """Если API отвечает, что файла нет, запись выбрасывают - будет перевыгрузка."""
    entry = remember(gemini, expires_in=CLOSE)
    del gemini.remote_files[entry.file.name]

    check(gemini)

    assert gemini.file_gets == [entry.file.name]
    assert not uploads_cache


def test_upload_in_a_wrong_state_is_forgotten(gemini, uploads_cache):
    """Файл, вернувшийся не в ACTIVE, тоже никуда не годится."""
    remember(gemini, expires_in=CLOSE, state="FAILED")

    check(gemini)

    assert not uploads_cache


def test_upload_that_dies_too_soon_is_dropped_after_the_check(gemini, uploads_cache):
    """
    Живой файл с истекающим сроком отправлять нельзя.

    Пока запрос соберется и дойдет до модели, такая ссылка успеет протухнуть - и вернет
    ровно тот 403, от которого весь сыр-бор. Дешевле выгрузить заново заранее.
    """
    entry = remember(gemini, expires_in=CLOSE)
    entry.file.expiration_time = FakeFile(expires_in=60).expiration_time

    check(gemini)

    assert gemini.file_gets == [entry.file.name]
    assert not uploads_cache


def test_expired_entries_of_other_files_are_swept(gemini, uploads_cache):
    """Просроченные записи уходят из кэша, даже когда обращаются не к ним."""
    remember(gemini)
    remember(gemini, media_path=OTHER_PATH, expires_in=-1)

    check(gemini)

    assert list(uploads_cache) == [(KEY_ONE, MEDIA_PATH)]


def test_recheck_forces_a_question_about_a_fresh_upload(gemini, uploads_cache):
    """После перепроверки даже свежая запись сверяется с API."""
    entry = remember(gemini)

    assert files.recheck_uploads() == 1

    check(gemini)
    assert gemini.file_gets == [entry.file.name]
    # Файл на месте - запись осталась, перевыгружать нечего.
    assert (KEY_ONE, MEDIA_PATH) in uploads_cache


def test_recheck_reuploads_only_what_really_died(gemini, uploads_cache):
    """Перепроверка выбрасывает мертвое и оставляет живое."""
    alive = remember(gemini)
    dead = remember(gemini, media_path=OTHER_PATH)
    del gemini.remote_files[dead.file.name]

    files.recheck_uploads()
    check(gemini)
    check(gemini, media_path=OTHER_PATH)

    assert list(uploads_cache) == [(KEY_ONE, MEDIA_PATH)]
    assert gemini.file_gets == [alive.file.name, dead.file.name]


def test_recheck_can_be_limited_to_one_key(gemini, uploads_cache):
    """Перепроверка одного ключа не гонит в сеть выгрузки остальных."""
    remember(gemini, api_key=KEY_ONE)
    remember(gemini, api_key=KEY_TWO)

    assert files.recheck_uploads(KEY_ONE) == 1

    check(gemini, api_key=KEY_TWO)
    assert gemini.file_gets == []
    assert uploads_cache[(KEY_ONE, MEDIA_PATH)].suspect
    assert not uploads_cache[(KEY_TWO, MEDIA_PATH)].suspect


def test_upload_remembers_the_expiration_from_the_api(gemini, uploads_cache):
    """Срок берется из ответа Files API, а не выдумывается."""
    gemini.next_upload = FakeFile(name="files/свежий", expires_in=3 * 60 * 60)

    files.upload_file(gemini.client_for_key(KEY_ONE), KEY_ONE, MEDIA_PATH)

    assert uploads_cache[(KEY_ONE, MEDIA_PATH)].expires_at == pytest.approx(
        time.time() + 3 * 60 * 60, abs=60
    )


def test_upload_without_expiration_falls_back_to_the_documented_ttl(gemini, uploads_cache):
    """
    Без expiration_time срок берется по документации - 48 часов.

    Поле необязательное, и его отсутствие не означает, что файл вечный: считать его
    вечным - как раз и значит однажды отправить мертвую ссылку.
    """
    gemini.next_upload = FakeFile(name="files/безсрока", expires_in=None)

    files.upload_file(gemini.client_for_key(KEY_ONE), KEY_ONE, MEDIA_PATH)

    assert uploads_cache[(KEY_ONE, MEDIA_PATH)].expires_at == pytest.approx(
        time.time() + files.DEFAULT_UPLOAD_TTL, abs=60
    )


def media_message(path):
    """Собирает строку сообщения с вложением - такую же, как отдает база."""
    return {
        "content": "смотри что нашел",
        "username": "tester",
        "is_bot": 0,
        "file_id": "FILE",
        "mime_type": "image/png",
        "media_path": path,
    }


def test_fresh_upload_reaches_the_model_without_network(gemini, tmp_path):
    """Сборка запроса по свежему кэшу не делает ни одного обращения к Files API."""
    photo = tmp_path / "фото.png"
    photo.write_bytes(b"png")
    entry = remember(gemini, media_path=str(photo))

    parts = contents.build_message_parts(
        gemini.client_for_key(KEY_ONE), KEY_ONE, media_message(str(photo))
    )

    assert parts[1].file_data.file_uri == entry.file.uri
    assert gemini.file_gets == []
    assert gemini.file_uploads == []


def test_expired_upload_is_replaced_before_the_request(gemini, tmp_path):
    """Протухшая выгрузка заменяется новой, и в запрос уходит уже новая ссылка."""
    photo = tmp_path / "фото.png"
    photo.write_bytes(b"png")
    stale = remember(gemini, media_path=str(photo), expires_in=-1)
    gemini.next_upload = FakeFile(name="files/новая")

    parts = contents.build_message_parts(
        gemini.client_for_key(KEY_ONE), KEY_ONE, media_message(str(photo))
    )

    assert gemini.file_uploads == [str(photo)]
    assert parts[1].file_data.file_uri != stale.file.uri
    assert parts[1].file_data.file_uri == "https://files.example/files/новая"
