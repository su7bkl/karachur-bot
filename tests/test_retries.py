"""
Тесты цикла повторов: когда бот меняет ключ, когда ждет, а когда сдается.

Каждый сценарий - это заранее расписанные ответы Gemini на каждый ключ. Проверяется не
только итог, но и то, каким ключом бот ходил и пересобирал ли запрос: ссылки на
выгруженные файлы принадлежат ключу, и после ротации запрос обязан собраться заново.

Отдельная кучка тестов в конце - про рассказ наружу: цикл сообщает об этапах колбэком
progress, и проверяется именно структура (номер попытки, пауза, вид ошибки), а не текст.
Текста тут и не должно быть: во что этапы превратятся в чате, решает karachur.tg.status,
и его тесты живут отдельно, в test_status.py. Колбэк необязателен, поэтому все остальные
тесты файла заодно проверяют и второе обещание - без него все работает как раньше.
"""

import asyncio
import dataclasses
import time

import pytest

from conftest import CHAT_ONE, KEY_ONE, KEY_TWO, ApiError, FakeFile
from karachur.gemini import compress, errors, files, stages

# Модуль зовется request_contents, а не contents: имя contents тут занято самими
# собираемыми списками содержимого запроса - ровно как в karachur.gemini.answer.
from karachur.gemini import contents as request_contents

# Модуль зовется key_pool, а не pool: имя pool в тестах занято самим пулом чата.
from karachur.gemini import pool as key_pool
from karachur.gemini import retries
from karachur.storage import keys as key_store
from karachur.storage import messages, settings, summaries


def prepared_pool(conn, *keys, start_with=None, daily_limit=250, model="gemini-test"):
    """Заводит чату ключи и ставит указатель на нужный."""
    for key in keys:
        key_store.add_key(conn, CHAT_ONE, key)
    if start_with:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE api_key = ?", (start_with,)
        ).fetchone()
        settings.set_active_key_id(conn, CHAT_ONE, row[0])
    return key_pool.KeyPool(conn, CHAT_ONE, model, daily_limit)


def ask(cfg, pool, gemini, progress=None):
    """Прогоняет запрос через цикл повторов. Модель берется из самого пула."""
    return asyncio.run(
        retries.generate_with_retries(cfg, pool, gemini.make_contents, progress)
    )


def stage_recorder():
    """
    Отдает колбэк этапов и список, куда он их складывает.

    :return: (список этапов по порядку, сам колбэк)
    :rtype: tuple[list, typing.Callable]
    """
    seen = []

    async def record(stage):
        """Запоминает этап вместо того, чтобы что-то с ним делать."""
        seen.append(stage)

    return seen, record


def test_daily_quota_switches_key_and_rebuilds_request(cfg, db, gemini, api_error):
    """Выбранная дневная квота уводит запрос на следующий ключ."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.daily_quota())
    gemini.script(KEY_TWO, "ответ со второго ключа")

    answer = ask(cfg, pool, gemini)

    assert answer == "ответ со второго ключа"
    assert gemini.calls == [KEY_ONE, KEY_TWO]
    # Запрос собран дважды: выгрузки первого ключа второму не принадлежат.
    assert gemini.built_for == [KEY_ONE, KEY_TWO]


def test_spent_request_is_counted_on_the_key_that_answered(cfg, db, gemini, api_error):
    """Упавшая попытка счетчик не тратит, удачная - тратит."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.daily_quota())
    gemini.script(KEY_TWO, "готово")

    ask(cfg, pool, gemini)

    spent = dict(
        db.execute(
            """
            SELECT k.api_key, COALESCE(q.requests_today, 0) FROM api_keys k
            LEFT JOIN key_quota q ON q.key_id = k.id
            """
        ).fetchall()
    )
    assert spent[KEY_ONE] == 0
    assert spent[KEY_TWO] == 1


def test_rate_limit_keeps_the_same_key(cfg, db, gemini, api_error):
    """На минутном лимите бот ждет, а ключ не жжет."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.rate_limit(), "ответ после паузы")

    answer = ask(cfg, pool, gemini)

    assert answer == "ответ после паузы"
    assert gemini.calls == [KEY_ONE, KEY_ONE]
    exhausted = db.execute("SELECT COUNT(*) FROM key_quota WHERE daily_exhausted = 1")
    assert exhausted.fetchone()[0] == 0


def test_rejected_key_is_marked_and_replaced(cfg, db, gemini, api_error):
    """Отклоненный ключ помечается с причиной, а запрос уходит на следующий."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.bad_key())
    gemini.script(KEY_TWO, "ответ живого ключа")

    answer = ask(cfg, pool, gemini)

    assert answer == "ответ живого ключа"
    reason = db.execute(
        "SELECT broken_reason FROM api_keys WHERE api_key = ?", (KEY_ONE,)
    ).fetchone()[0]
    assert "PERMISSION_DENIED" in reason


def test_fatal_error_does_not_burn_the_pool(cfg, db, gemini, api_error):
    """Несуществующая модель обрывает попытки, не перебирая ключи."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error(404, "models/nope is not found"))
    gemini.script(KEY_TWO, "не должно понадобиться")

    with pytest.raises(retries.GeminiRetryError, match="404"):
        ask(cfg, pool, gemini)

    assert gemini.calls == [KEY_ONE]
    intact = db.execute(
        "SELECT COUNT(*) FROM api_keys WHERE broken_reason IS NULL"
    ).fetchone()[0]
    assert intact == 2


def test_stale_file_error_spares_the_key(cfg, db, gemini, api_error):
    """
    Ошибка про пропавший файл не трогает ключ - ни mark_broken, ни выбранной квоты.

    Ради этого все и затевалось. Пока такой 403 читался как отказ по ключу, бот на
    каждую мертвую ссылку хоронил очередной ключ чата, брал следующий, отправлял ту же
    ссылку - и хоронил и его. Одной старой картинки в истории хватало на весь пул.
    """
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.stale_file(), "ответ после перевыгрузки")

    answer = ask(cfg, pool, gemini)

    assert answer == "ответ после перевыгрузки"
    # Ключ не сменился: менять его было не на что и незачем.
    assert gemini.calls == [KEY_ONE, KEY_ONE]
    intact = db.execute(
        "SELECT COUNT(*) FROM api_keys WHERE broken_reason IS NULL"
    ).fetchone()[0]
    assert intact == 2
    exhausted = db.execute(
        "SELECT COUNT(*) FROM key_quota WHERE daily_exhausted = 1"
    ).fetchone()[0]
    assert exhausted == 0


def test_stale_file_error_rebuilds_the_request(cfg, db, gemini, api_error):
    """
    После ошибки про файл запрос собирается заново, а не уходит теми же ссылками.

    Ключ при этом тот же, а обычно запрос пересобирается только при смене ключа - без
    отдельного сброса повтор отправил бы ровно ту мертвую ссылку, на которой споткнулся.
    """
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.stale_file(), "ответ после перевыгрузки")

    ask(cfg, pool, gemini)

    assert gemini.built_for == [KEY_ONE, KEY_ONE]


def test_stale_file_error_sends_the_cache_for_recheck(cfg, db, gemini, api_error):
    """Кэш выгрузок после такой ошибки помечается на перепроверку."""
    entry = files.Upload(file=FakeFile(), expires_at=time.time() + 10 * 60 * 60)
    files.uploaded_files[(KEY_ONE, "/media/фото.png")] = entry
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.stale_file(), "готово")

    ask(cfg, pool, gemini)

    assert entry.suspect


def test_dead_link_is_replaced_by_a_live_one(cfg, db, gemini, tmp_path):
    """
    Сквозная проверка: после отказа по файлу второй запрос уходит с новой ссылкой.

    Здесь запрос собирается не подделкой, а настоящей сборкой, поэтому видно то, ради
    чего все и делалось: в первом запросе уезжает мертвая ссылка из кэша, в ответ
    прилетает 403 про File, а во втором на ее месте оказывается свежая выгрузка - тем же
    ключом, без всякой ротации.
    """
    photo = tmp_path / "фото.png"
    photo.write_bytes(b"png")
    # Выгрузка, которой на стороне Google уже нет: в gemini.remote_files ее не кладем.
    stale = FakeFile(name="files/протухшая")
    files.uploaded_files[(KEY_ONE, str(photo))] = files.Upload(
        file=stale, expires_at=time.time() + 10 * 60 * 60
    )
    message = {
        "username": "tester",
        "content": "лови картинку",
        "file_id": "FILEID",
        "mime_type": "image/png",
        "media_path": str(photo),
    }
    gemini.next_upload = FakeFile(name="files/новая")

    async def make_contents(key):
        """Собирает запрос так же, как это делает karachur.gemini.answer."""
        history = request_contents.build_history(key, [message], cfg.media_dir)
        return request_contents.build_contents(history, None, cfg.system_prompt)

    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, ApiError.stale_file(), "разглядел картинку")

    answer = asyncio.run(retries.generate_with_retries(cfg, pool, make_contents))

    assert answer == "разглядел картинку"
    sent = [request[-1]["parts"][-1].file_data.file_uri for request in gemini.contents_sent]
    assert sent == [stale.uri, "https://files.example/files/новая"]


def test_ordinary_failure_reuses_the_request(cfg, db, gemini, api_error):
    """
    Обычный сбой запрос не пересобирает.

    Обратная сторона той же границы: сборка ходит в Files API по каждому вложению
    истории, и повторять ее из-за пятисотки незачем.
    """
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error(500, "INTERNAL"), "получилось со второй")

    ask(cfg, pool, gemini)

    assert gemini.built_for == [KEY_ONE]


def test_transient_error_is_retried(cfg, db, gemini, api_error):
    """Временный сбой повторяется тем же ключом."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error(500, "INTERNAL"), "получилось со второй")

    assert ask(cfg, pool, gemini) == "получилось со второй"
    assert gemini.calls == [KEY_ONE, KEY_ONE]


def test_attempts_run_out(cfg, db, gemini, api_error):
    """Когда попытки кончились, бот честно говорит, на чем остановился."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, *[api_error(500, "INTERNAL")] * cfg.max_retries)

    with pytest.raises(retries.GeminiRetryError, match="Не удалось получить ответ"):
        ask(cfg, pool, gemini)


def test_empty_answer_is_retried(cfg, db, gemini):
    """Пустой ответ модели - повод повторить, а не сдаться."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, "", "теперь с текстом")

    assert ask(cfg, pool, gemini) == "теперь с текстом"


def test_exhausted_pool_stops_the_request(cfg, db, gemini, api_error):
    """Когда рабочих ключей не осталось, наружу уходит понятный отказ."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.daily_quota())

    with pytest.raises(key_pool.NoUsableKeys, match="/keys"):
        ask(cfg, pool, gemini)


def test_chosen_model_reaches_the_api(cfg, db, gemini):
    """В запрос уходит модель, выбранная чатом."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE, model="gemini-3-pro")
    gemini.script(KEY_ONE, "готово")

    ask(cfg, pool, gemini)

    assert gemini.models_used == ["gemini-3-pro"]


def test_context_is_compressed_and_saved(cfg, db, gemini, add_message):
    """Разросшийся контекст сжимается, а пересказ ложится в базу своего чата."""
    # Настройки замороженные, поэтому лимит не подменяется, а задается своей копией.
    cfg = dataclasses.replace(cfg, max_context_tokens=1000)
    for number in range(1, 16):
        add_message(CHAT_ONE, number, f"сообщение номер {number} " + "текст " * 40)

    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    # Первый подсчет не влезает в лимит, после сжатия - влезает.
    gemini.token_counts = [2000, 400]
    gemini.script(KEY_ONE, "пересказ старой части")

    _, context = messages.get_context(db, CHAT_ONE)
    kept, summary = asyncio.run(
        compress.compress_context(cfg, pool, db, CHAT_ONE, context, None)
    )

    assert summary == "пересказ старой части"
    assert len(kept) < len(context)
    assert summaries.get_latest_summary(db, CHAT_ONE) == "пересказ старой части"
    compressed = db.execute(
        "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND summarized = 1", (CHAT_ONE,)
    ).fetchone()[0]
    assert compressed == len(context) - len(kept)


def test_small_context_is_left_alone(cfg, db, gemini, add_message):
    """Короткая история до сжатия не доходит и лишних запросов не делает."""
    add_message(CHAT_ONE, 1, "короткое сообщение")
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)

    _, context = messages.get_context(db, CHAT_ONE)
    kept, summary = asyncio.run(
        compress.compress_context(cfg, pool, db, CHAT_ONE, context, None)
    )

    assert kept == context
    assert summary is None
    assert not gemini.calls


def test_progress_tells_about_each_attempt_and_pause(cfg, db, gemini, api_error):
    """
    Наружу уходят и сам запрос, и повтор после него - с номером попытки и паузой.

    Именно эти две цифры человек в чате и ждет: без них "бот думает" неотличимо от
    "бот завис".
    """
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.rate_limit(), "получилось со второй")
    seen, progress = stage_recorder()

    ask(cfg, pool, gemini, progress)

    assert [stage.name for stage in seen] == [
        stages.ASKING,
        stages.RETRY,
        stages.ASKING,
    ]
    retry = seen[1]
    assert (retry.number, retry.total) == (2, cfg.max_retries)
    assert retry.delay > 0
    # Причина уезжает константой, а не текстом: переводить ее на человеческий - работа
    # слоя tg, и слой gemini в это не лезет.
    assert retry.error_kind == errors.ERROR_KIND_RATE


def test_progress_does_not_promise_a_pause_before_key_change(
    cfg, db, gemini, api_error
):
    """
    Смена выдохшегося ключа идет без паузы, и в этапе ее нет.

    Обещать в чате ожидание, которого не будет, - тот же обман, что и молчание.
    """
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.daily_quota())
    gemini.script(KEY_TWO, "ответ со второго ключа")
    seen, progress = stage_recorder()

    ask(cfg, pool, gemini, progress)

    retry = next(stage for stage in seen if stage.name == stages.RETRY)
    assert retry.delay is None
    assert retry.error_kind == errors.ERROR_KIND_DAILY


def test_progress_does_not_promise_an_attempt_that_never_comes(
    cfg, db, gemini, api_error
):
    """
    После последней попытки повтор не обещается: следующей не будет, будет ошибка.

    Иначе заглушка застыла бы на "попытке 5 из 4", пока доставка не заменит ее текстом
    отказа.
    """
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, *[api_error(500, "INTERNAL")] * cfg.max_retries)
    seen, progress = stage_recorder()

    with pytest.raises(retries.GeminiRetryError):
        ask(cfg, pool, gemini, progress)

    retries_reported = [stage for stage in seen if stage.name == stages.RETRY]
    assert len(retries_reported) == cfg.max_retries - 1
    assert seen[-1].name == stages.ASKING
    assert max(stage.number for stage in retries_reported) == cfg.max_retries


def test_progress_is_optional(cfg, db, gemini, api_error):
    """
    Без колбэка цикл работает ровно как раньше - ни лишних движений, ни падений.

    Так его зовут тесты и любой не-телеграмный вызов, и добавление этапов их не касается.
    """
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.rate_limit(), "получилось со второй")

    assert ask(cfg, pool, gemini) == "получилось со второй"
    assert gemini.calls == [KEY_ONE, KEY_ONE]


def test_progress_tells_about_compression_rounds(cfg, db, gemini, add_message):
    """
    Сжатие называет свой проход: это отдельный полноценный запрос к модели, и молчать
    о нем - значит оставить человека без объяснения самой долгой части ожидания.

    Заодно видно и соседние этапы: выгрузку вложений (build_history) и точный подсчет
    токенов - оба ходят в сеть и оба идут до самого пересказа.
    """
    # Настройки замороженные, поэтому лимит не подменяется, а задается своей копией.
    cfg = dataclasses.replace(cfg, max_context_tokens=1000)
    for number in range(1, 16):
        add_message(CHAT_ONE, number, f"сообщение номер {number} " + "текст " * 40)

    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    # Первый подсчет не влезает в лимит, после сжатия - влезает.
    gemini.token_counts = [2000, 400]
    gemini.script(KEY_ONE, "пересказ старой части")
    seen, progress = stage_recorder()

    _, context = messages.get_context(db, CHAT_ONE)
    asyncio.run(
        compress.compress_context(cfg, pool, db, CHAT_ONE, context, None, progress)
    )

    names = [stage.name for stage in seen]
    assert names[:3] == [stages.ATTACHMENTS, stages.MEASURING, stages.COMPRESS]
    round_stage = seen[2]
    assert (round_stage.number, round_stage.total) == (1, cfg.max_compression_rounds)
    # Пересказ - такой же запрос к модели, и о нем тоже рассказывают.
    assert stages.ASKING in names
