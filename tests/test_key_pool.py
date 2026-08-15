"""
Тесты пула ключей: владение, счетчики, выбытие и ротация.

Отдельно проверяется, что счетчик общий для всех чатов с одним и тем же ключом, но
раздельный для разных моделей: квоту Google считает на пару "проект и модель".
"""

import pytest

import api_keys
import chat_settings
from conftest import (
    CHAT_ONE,
    CHAT_TWO,
    KEY_ONE,
    KEY_THREE,
    KEY_TWO,
    MODEL,
    OTHER_MODEL,
    SHARED_KEY,
)


def pool_for(conn, chat_id, daily_limit=250, model=MODEL):
    """Собирает пул ключей чата под указанную модель."""
    return api_keys.KeyPool(conn, chat_id, model, daily_limit)


def test_keys_belong_to_their_chat(db):
    """Ключ виден только тому чату, которому его добавили."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_ONE, KEY_TWO)
    api_keys.add_key(db, CHAT_TWO, KEY_THREE)

    assert len(api_keys.list_chat_keys(db, CHAT_ONE, MODEL)) == 2
    assert [key["api_key"] for key in api_keys.list_chat_keys(db, CHAT_TWO, MODEL)] == [
        KEY_THREE
    ]


def test_adding_the_same_key_twice_is_noticed(db):
    """Повторное добавление ключа не плодит дублей."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    _, already = api_keys.add_key(db, CHAT_ONE, KEY_ONE)

    assert already
    assert len(api_keys.list_chat_keys(db, CHAT_ONE, MODEL)) == 1


def test_counter_is_shared_between_chats(db):
    """У одного ключа в двух чатах общий дневной счетчик."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_TWO, KEY_ONE)

    pool_one, pool_two = pool_for(db, CHAT_ONE), pool_for(db, CHAT_TWO)
    pool_one.note_request(pool_one.active())
    pool_two.note_request(pool_two.active())

    spent = db.execute(
        """
        SELECT q.requests_today FROM key_quota q
        JOIN api_keys k ON k.id = q.key_id
        WHERE k.api_key = ? AND q.model = ?
        """,
        (KEY_ONE, MODEL),
    ).fetchone()[0]
    assert spent == 2


def test_daily_quota_moves_chat_to_the_next_key(db):
    """Выбранная дневная квота уводит чат на следующий ключ."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_ONE, KEY_TWO)
    pool = pool_for(db, CHAT_ONE)

    pool.mark_daily_exhausted(pool.active())

    assert pool.active()["api_key"] == KEY_TWO


def test_active_key_survives_restart(db):
    """Указатель активного ключа живет в базе, а не в памяти пула."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_ONE, KEY_TWO)
    pool = pool_for(db, CHAT_ONE)
    pool.mark_daily_exhausted(pool.active())
    pool.active()

    # Новый пул - как после перезапуска бота: ничего, кроме базы, не осталось.
    assert pool_for(db, CHAT_ONE).active()["api_key"] == KEY_TWO
    assert chat_settings.get_active_key_id(db, CHAT_ONE) is not None


def test_local_limit_does_not_block_the_last_live_key(db):
    """Собственный счетчик - страховка: при живой квоте он не запрещает работу."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    pool = pool_for(db, CHAT_ONE, daily_limit=2)

    key = pool.active()
    for _ in range(5):
        pool.note_request(key)

    assert pool.active()["api_key"] == KEY_ONE


def test_local_limit_prefers_a_fresher_key(db):
    """При выборе бот сначала обходит ключи, не выбравшие местный лимит."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_ONE, KEY_TWO)
    pool = pool_for(db, CHAT_ONE, daily_limit=2)

    first = pool.active()
    for _ in range(2):
        pool.note_request(first)

    assert pool.active()["api_key"] == KEY_TWO


def test_broken_key_is_skipped(db):
    """Отклоненный API ключ выбывает вместе с причиной."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_ONE, KEY_TWO)
    pool = pool_for(db, CHAT_ONE)

    pool.mark_broken(pool.active(), "403 PERMISSION_DENIED")

    assert pool.active()["api_key"] == KEY_TWO
    broken = db.execute(
        "SELECT broken_reason FROM api_keys WHERE api_key = ?", (KEY_ONE,)
    ).fetchone()[0]
    assert "PERMISSION_DENIED" in broken


def test_empty_pool_explains_itself(db):
    """Чат без ключей получает подсказку, а не отказ без объяснений."""
    with pytest.raises(api_keys.NoUsableKeys, match="/addkey"):
        pool_for(db, CHAT_ONE).active()


def test_dead_pool_reports_what_happened(db):
    """Исчерпанный пул объясняет, кто выбыл по квоте, а кто отклонен."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_ONE, KEY_TWO)
    pool = pool_for(db, CHAT_ONE)
    pool.mark_daily_exhausted(pool.active())
    pool.mark_broken(pool.active(), "403 PERMISSION_DENIED")

    with pytest.raises(api_keys.NoUsableKeys) as failure:
        pool.active()

    message = str(failure.value)
    assert "кончилась дневная квота" in message
    assert "отклонены API" in message


def test_rotate_moves_off_the_current_key(db):
    """Принудительная ротация уходит именно с текущего ключа."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_ONE, KEY_TWO)
    pool = pool_for(db, CHAT_ONE)
    pool.active()

    rotated = pool.rotate("тест")

    assert rotated["api_key"] == KEY_TWO
    assert pool.active()["api_key"] == KEY_TWO


def test_rotate_without_spare_keys_returns_nothing(db):
    """Ротировать единственный ключ не на что."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)

    assert pool_for(db, CHAT_ONE).rotate("тест") is None


def test_removed_key_disappears_with_its_counters(db):
    """Ключ, выпавший из всех чатов, удаляется вместе со счетчиками."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    key = api_keys.list_chat_keys(db, CHAT_ONE, MODEL)[0]

    api_keys.remove_key(db, CHAT_ONE, key)

    left = db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
    assert left == 0


def test_key_used_elsewhere_survives_removal(db):
    """Ключ, оставшийся в другом чате, при удалении не пропадает."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_TWO, KEY_ONE)
    key = api_keys.list_chat_keys(db, CHAT_ONE, MODEL)[0]

    api_keys.remove_key(db, CHAT_ONE, key)

    assert not api_keys.list_chat_keys(db, CHAT_ONE, MODEL)
    assert len(api_keys.list_chat_keys(db, CHAT_TWO, MODEL)) == 1


def test_shared_key_is_available_everywhere(db):
    """Общий ключ из конфига виден всем чатам и помечен как общий."""
    api_keys.sync_shared_key(db, SHARED_KEY)

    keys = api_keys.list_chat_keys(db, CHAT_TWO, MODEL)

    assert [key["api_key"] for key in keys] == [SHARED_KEY]
    assert keys[0]["owner_chat_id"] == api_keys.SHARED_CHAT_ID


def test_shared_key_follows_the_config(db):
    """Замена ключа в конфиге отвязывает прежний общий ключ."""
    api_keys.sync_shared_key(db, SHARED_KEY)
    api_keys.sync_shared_key(db, KEY_TWO)

    keys = api_keys.list_chat_keys(db, CHAT_ONE, MODEL)

    assert [key["api_key"] for key in keys] == [KEY_TWO]


def test_own_keys_come_before_the_shared_one(db):
    """Свои ключи чат тратит раньше общего."""
    api_keys.sync_shared_key(db, SHARED_KEY)
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)

    assert pool_for(db, CHAT_ONE).active()["api_key"] == KEY_ONE


def test_yesterday_counters_are_reset(db):
    """Счетчики прошлых суток обнуляются при первом же обращении."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    pool = pool_for(db, CHAT_ONE)
    pool.note_request(pool.active())
    db.execute("UPDATE key_quota SET quota_date = '2020-01-01', requests_today = 99")
    db.commit()

    key = api_keys.list_chat_keys(db, CHAT_ONE, MODEL)[0]

    assert key["requests_today"] == 0
    assert not key["daily_exhausted"]


def found_key(keys, reference):
    """Ищет ключ по ссылке и возвращает сам ключ или None."""
    found = api_keys.find_key(keys, reference)
    return found["api_key"] if found else None


def test_key_is_found_by_number_and_by_tail(db):
    """Ключ находится и по номеру в списке, и по хвосту самого ключа."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    api_keys.add_key(db, CHAT_ONE, KEY_TWO)
    keys = api_keys.list_chat_keys(db, CHAT_ONE, MODEL)

    assert found_key(keys, "2") == KEY_TWO
    assert found_key(keys, KEY_ONE[-6:]) == KEY_ONE
    assert found_key(keys, "99") is None
    assert found_key(keys, "не ключ") is None


def test_masked_key_hides_the_middle():
    """В чат и логи уходят только концы ключа."""
    masked = api_keys.mask_key(KEY_ONE)

    assert KEY_ONE not in masked
    assert masked.endswith(KEY_ONE[-4:])


@pytest.mark.parametrize(
    "code, message, expected",
    [
        (429, "quotaId: GenerateRequestsPerDayPerProjectPerModel", "daily"),
        (429, "quotaId: GenerateRequestsPerMinute retryDelay: 27s", "rate"),
        (403, "PERMISSION_DENIED", "key"),
        (401, "UNAUTHENTICATED", "key"),
        (400, "API key not valid. Please pass a valid API key.", "key"),
        (400, "Requests ending with a model turn are not supported", "fatal"),
        (404, "models/nope is not found", "fatal"),
        (500, "INTERNAL", "transient"),
        (503, "UNAVAILABLE", "transient"),
    ],
)
def test_api_errors_are_classified(api_error, code, message, expected):
    """Ошибка API разбирается по коду и деталям квоты."""
    assert api_keys.classify_api_error(api_error(code, message)) == expected


def test_connection_errors_are_retryable():
    """Ошибка без кода - обрыв связи или таймаут - считается временной."""
    assert api_keys.classify_api_error(TimeoutError("read timeout")) == "transient"


def test_quota_is_counted_per_model(db):
    """Счетчики одного ключа не смешиваются между моделями."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    first = pool_for(db, CHAT_ONE, model=MODEL)
    second = pool_for(db, CHAT_ONE, model=OTHER_MODEL)

    first.note_request(first.active())
    first.note_request(first.active())
    second.note_request(second.active())

    assert first.keys()[0]["requests_today"] == 2
    assert second.keys()[0]["requests_today"] == 1


def test_exhausted_model_does_not_disable_the_key_elsewhere(db):
    """Кончившаяся квота одной модели не выводит ключ из строя на других."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    spent = pool_for(db, CHAT_ONE, model="модель-без-квоты")
    spent.mark_daily_exhausted(spent.active())

    # На модели без квоты ключей не осталось...
    with pytest.raises(api_keys.NoUsableKeys):
        spent.active()
    # ...а на рабочей модели тот же ключ по-прежнему годится.
    assert pool_for(db, CHAT_ONE, model=MODEL).active()["api_key"] == KEY_ONE


def test_model_without_quota_does_not_burn_the_whole_pool(db):
    """Модель без бесплатной квоты не должна выжигать весь пул чата до полуночи."""
    for key in (KEY_ONE, KEY_TWO, KEY_THREE):
        api_keys.add_key(db, CHAT_ONE, key)

    # Все три ключа спотыкаются об одну и ту же модель без квоты.
    doomed = pool_for(db, CHAT_ONE, model="модель-без-квоты")
    for _ in range(3):
        doomed.mark_daily_exhausted(doomed.active())

    with pytest.raises(api_keys.NoUsableKeys):
        doomed.active()

    working = pool_for(db, CHAT_ONE, model=MODEL)
    assert len([key for key in working.keys() if working.is_usable(key)]) == 3


def test_dead_pool_names_the_model(db):
    """Отказ называет модель: дело может быть в ней, а не в ключах."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    pool = pool_for(db, CHAT_ONE, model="модель-без-квоты")
    pool.mark_daily_exhausted(pool.active())

    with pytest.raises(api_keys.NoUsableKeys) as failure:
        pool.active()

    message = str(failure.value)
    assert "модель-без-квоты" in message
    assert "/model" in message


def test_broken_key_stays_broken_for_every_model(db):
    """Отказ по самому ключу от модели не зависит."""
    api_keys.add_key(db, CHAT_ONE, KEY_ONE)
    pool = pool_for(db, CHAT_ONE, model=MODEL)
    pool.mark_broken(pool.active(), "403 PERMISSION_DENIED")

    with pytest.raises(api_keys.NoUsableKeys):
        pool_for(db, CHAT_ONE, model=OTHER_MODEL).active()
