"""
Тесты цикла повторов: когда бот меняет ключ, когда ждет, а когда сдается.

Каждый сценарий - это заранее расписанные ответы Gemini на каждый ключ. Проверяется не
только итог, но и то, каким ключом бот ходил и пересобирал ли запрос: ссылки на
выгруженные файлы принадлежат ключу, и после ротации запрос обязан собраться заново.
"""

import asyncio

import pytest

import api_keys
import bot
import chat_settings
from conftest import CHAT_ONE, KEY_ONE, KEY_TWO


def prepared_pool(conn, *keys, start_with=None, daily_limit=250, model="gemini-test"):
    """Заводит чату ключи и ставит указатель на нужный."""
    for key in keys:
        api_keys.add_key(conn, CHAT_ONE, key)
    if start_with:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE api_key = ?", (start_with,)
        ).fetchone()
        chat_settings.set_active_key_id(conn, CHAT_ONE, row[0])
    return api_keys.KeyPool(conn, CHAT_ONE, model, daily_limit)


def ask(pool, gemini):
    """Прогоняет запрос через цикл повторов. Модель берется из самого пула."""
    return asyncio.run(bot.generate_with_retries(pool, gemini.make_contents))


def test_daily_quota_switches_key_and_rebuilds_request(db, gemini, api_error):
    """Выбранная дневная квота уводит запрос на следующий ключ."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.daily_quota())
    gemini.script(KEY_TWO, "ответ со второго ключа")

    answer = ask(pool, gemini)

    assert answer == "ответ со второго ключа"
    assert gemini.calls == [KEY_ONE, KEY_TWO]
    # Запрос собран дважды: выгрузки первого ключа второму не принадлежат.
    assert gemini.built_for == [KEY_ONE, KEY_TWO]


def test_spent_request_is_counted_on_the_key_that_answered(db, gemini, api_error):
    """Упавшая попытка счетчик не тратит, удачная - тратит."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.daily_quota())
    gemini.script(KEY_TWO, "готово")

    ask(pool, gemini)

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


def test_rate_limit_keeps_the_same_key(db, gemini, api_error):
    """На минутном лимите бот ждет, а ключ не жжет."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.rate_limit(), "ответ после паузы")

    answer = ask(pool, gemini)

    assert answer == "ответ после паузы"
    assert gemini.calls == [KEY_ONE, KEY_ONE]
    exhausted = db.execute("SELECT COUNT(*) FROM key_quota WHERE daily_exhausted = 1")
    assert exhausted.fetchone()[0] == 0


def test_rejected_key_is_marked_and_replaced(db, gemini, api_error):
    """Отклоненный ключ помечается с причиной, а запрос уходит на следующий."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.bad_key())
    gemini.script(KEY_TWO, "ответ живого ключа")

    answer = ask(pool, gemini)

    assert answer == "ответ живого ключа"
    reason = db.execute(
        "SELECT broken_reason FROM api_keys WHERE api_key = ?", (KEY_ONE,)
    ).fetchone()[0]
    assert "PERMISSION_DENIED" in reason


def test_fatal_error_does_not_burn_the_pool(db, gemini, api_error):
    """Несуществующая модель обрывает попытки, не перебирая ключи."""
    pool = prepared_pool(db, KEY_ONE, KEY_TWO, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error(404, "models/nope is not found"))
    gemini.script(KEY_TWO, "не должно понадобиться")

    with pytest.raises(bot.GeminiRetryError, match="404"):
        ask(pool, gemini)

    assert gemini.calls == [KEY_ONE]
    intact = db.execute(
        "SELECT COUNT(*) FROM api_keys WHERE broken_reason IS NULL"
    ).fetchone()[0]
    assert intact == 2


def test_transient_error_is_retried(db, gemini, api_error):
    """Временный сбой повторяется тем же ключом."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error(500, "INTERNAL"), "получилось со второй")

    assert ask(pool, gemini) == "получилось со второй"
    assert gemini.calls == [KEY_ONE, KEY_ONE]


def test_attempts_run_out(db, gemini, api_error):
    """Когда попытки кончились, бот честно говорит, на чем остановился."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, *[api_error(500, "INTERNAL")] * bot.MAX_RETRIES)

    with pytest.raises(bot.GeminiRetryError, match="Не удалось получить ответ"):
        ask(pool, gemini)


def test_empty_answer_is_retried(db, gemini):
    """Пустой ответ модели - повод повторить, а не сдаться."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, "", "теперь с текстом")

    assert ask(pool, gemini) == "теперь с текстом"


def test_exhausted_pool_stops_the_request(db, gemini, api_error):
    """Когда рабочих ключей не осталось, наружу уходит понятный отказ."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    gemini.script(KEY_ONE, api_error.daily_quota())

    with pytest.raises(api_keys.NoUsableKeys, match="/keys"):
        ask(pool, gemini)


def test_chosen_model_reaches_the_api(db, gemini):
    """В запрос уходит модель, выбранная чатом."""
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE, model="gemini-3-pro")
    gemini.script(KEY_ONE, "готово")

    ask(pool, gemini)

    assert gemini.models_used == ["gemini-3-pro"]


def test_context_is_compressed_and_saved(db, gemini, add_message, monkeypatch):
    """Разросшийся контекст сжимается, а пересказ ложится в базу своего чата."""
    monkeypatch.setattr(bot, "MAX_CONTEXT_TOKENS", 1000)
    for number in range(1, 16):
        add_message(CHAT_ONE, number, f"сообщение номер {number} " + "текст " * 40)

    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)
    # Первый подсчет не влезает в лимит, после сжатия - влезает.
    gemini.token_counts = [2000, 400]
    gemini.script(KEY_ONE, "пересказ старой части")

    _, messages = bot.get_context(db, CHAT_ONE)
    kept, summary = asyncio.run(
        bot.compress_context(pool, db, CHAT_ONE, messages, None)
    )

    assert summary == "пересказ старой части"
    assert len(kept) < len(messages)
    assert bot.get_latest_summary(db, CHAT_ONE) == "пересказ старой части"
    compressed = db.execute(
        "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND summarized = 1", (CHAT_ONE,)
    ).fetchone()[0]
    assert compressed == len(messages) - len(kept)


def test_small_context_is_left_alone(db, gemini, add_message):
    """Короткая история до сжатия не доходит и лишних запросов не делает."""
    add_message(CHAT_ONE, 1, "короткое сообщение")
    pool = prepared_pool(db, KEY_ONE, start_with=KEY_ONE)

    _, messages = bot.get_context(db, CHAT_ONE)
    kept, summary = asyncio.run(
        bot.compress_context(pool, db, CHAT_ONE, messages, None)
    )

    assert kept == messages
    assert summary is None
    assert not gemini.calls
