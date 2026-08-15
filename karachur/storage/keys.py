"""
Хранение ключей Gemini: таблицы ключей, их привязка к чатам и дневные счетчики.

Здесь только работа с базой - что лежит в таблицах и как это читать и править. Выбор
ключа для очередного запроса, ротация и разбор ошибок API остаются в karachur.gemini:
это уже не хранение, а поведение, и меняются они по разным поводам.

Сам ключ хранится один раз на всю базу (api_keys), а привязка к чатам лежит отдельно
(chat_keys). Так сделано потому, что дневную квоту Google считает на ключ, а не на чат:
добавь один и тот же ключ в два чата - и счетчик у него все равно должен быть общий,
иначе бот будет думать, что квоты вдвое больше, чем есть.

Счетчики (key_quota) ведутся не на ключ, а на пару "ключ и модель": квота у Gemini своя
на каждую модель. Отсюда же и тихоокеанский часовой пояс - в его полночь Google обнуляет
дневные счетчики, и по нему же считается, за какие сутки записан счетчик.

Соединение отдает строки как sqlite3.Row (см. karachur.storage.db), поэтому наружу
выборки уходят через db.fetch_dicts: прочитанные ключи дальше правят на месте (сбрасывают
устаревшие счетчики, отмечают потраченный запрос), а Row - только для чтения.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from karachur.storage import db

logger = logging.getLogger(__name__)

# Ключ из config.cfg доступен всем чатам сразу. Привязываем его к несуществующему чату 0:
# такой chat_id Telegram не выдает, зато выборка ключей чата остается одним запросом без
# особого случая для общего ключа.
SHARED_CHAT_ID = 0

# Дневные квоты Gemini обнуляются в полночь по тихоокеанскому времени.
try:
    QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
except KeyError:  # в системе нет базы часовых поясов - берем стандартное смещение
    QUOTA_TIMEZONE = timezone(timedelta(hours=-8))


def init_key_tables(cursor: sqlite3.Cursor):
    """
    Создает таблицы ключей, если их еще нет.

    :param cursor: курсор открытой базы
    :type cursor: sqlite3.Cursor
    """
    # В самом ключе лежит только то, что от модели не зависит: отказ API - беда ключа
    # целиком, а вот квоты и счетчики живут отдельно, в key_quota.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT UNIQUE NOT NULL,
            broken_reason TEXT,
            added_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_keys (
            chat_id INTEGER NOT NULL,
            key_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            added_at TEXT,
            PRIMARY KEY (chat_id, key_id)
        )
    """)
    # Дневную квоту Gemini считает на пару "проект и модель" - это видно и по имени
    # квоты в ошибке: GenerateRequestsPerDayPerProjectPerModel. Поэтому счетчики ведутся
    # на пару "ключ и модель": исчерпанная квота одной модели ничего не говорит о
    # других, и помечать из-за нее весь ключ нельзя.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS key_quota (
            key_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            quota_date TEXT,
            requests_today INTEGER DEFAULT 0,
            daily_exhausted INTEGER DEFAULT 0,
            PRIMARY KEY (key_id, model)
        )
    """)


def mask_key(api_key: str) -> str:
    """
    Прячет ключ для показа в чате: от него остаются только концы.

    :param api_key: ключ Gemini
    :type api_key: str
    :return: замаскированный ключ
    :rtype: str
    """
    if len(api_key) <= 12:
        return "…" * 3
    return f"{api_key[:6]}…{api_key[-4:]}"


def quota_date() -> str:
    """
    Возвращает текущую дату в тихоокеанском поясе - сутки, за которые Google считает квоту.

    :return: дата в формате ГГГГ-ММ-ДД
    :rtype: str
    """
    return datetime.now(QUOTA_TIMEZONE).date().isoformat()


def next_quota_reset() -> datetime:
    """
    Возвращает момент ближайшего обнуления дневных квот.

    :return: ближайшая полночь по тихоокеанскому времени
    :rtype: datetime
    """
    now = datetime.now(QUOTA_TIMEZONE)
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), QUOTA_TIMEZONE)


def describe_quota_reset() -> str:
    """
    Описывает, когда ключи снова заработают, в местном времени машины.

    :return: время сброса квот словами
    :rtype: str
    """
    reset = next_quota_reset().astimezone()
    return reset.strftime("%H:%M %d.%m")


def _reset_stale_days(conn: sqlite3.Connection, keys: list[dict], model: str):
    """
    Обнуляет счетчики, у которых записанные сутки уже прошли.

    Сбрасываем лениво, при чтении: будильника на полночь у бота нет, а свежий счетчик
    нужен ровно в тот момент, когда ключ собираются использовать.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param keys: прочитанные ключи; устаревшие поля правятся прямо в них
    :type keys: list[dict]
    :param model: модель, к которой относятся счетчики
    :type model: str
    """
    today = quota_date()
    stale = [key for key in keys if key["quota_date"] and key["quota_date"] != today]
    if not stale:
        return

    conn.executemany(
        """
        UPDATE key_quota SET quota_date = ?, requests_today = 0, daily_exhausted = 0
        WHERE key_id = ? AND model = ?
        """,
        [(today, key["id"], model) for key in stale],
    )
    conn.commit()
    for key in stale:
        key.update(quota_date=today, requests_today=0, daily_exhausted=0)
    logger.info(
        "Счетчики %d ключей по модели %s обнулены: наступили новые сутки.",
        len(stale),
        model,
    )


def _linked_keys(conn: sqlite3.Connection, chat_id: int) -> list[dict]:
    """
    Возвращает ключи, привязанные к чату, без квот и счетчиков.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :return: строки с полями id и api_key
    :rtype: list[dict]
    """
    return db.fetch_dicts(
        conn.execute(
            """
            SELECT k.id, k.api_key FROM api_keys k
            JOIN chat_keys ck ON ck.key_id = k.id
            WHERE ck.chat_id = ?
            ORDER BY ck.position, k.id
            """,
            (chat_id,),
        )
    )


def list_chat_keys(conn: sqlite3.Connection, chat_id: int, model: str) -> list[dict]:
    """
    Возвращает ключи, доступные чату: сначала свои, потом общий из config.cfg.

    Счетчики и пометка об исчерпанной квоте подтягиваются под конкретную модель:
    квота у Gemini считается на пару "проект и модель", и у другой модели у того же
    ключа свои счетчики.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param model: модель, к которой относятся счетчики
    :type model: str
    :return: строки ключей с полями owner_chat_id, position и счетчиками по модели
    :rtype: list[dict]
    """
    # Колонки перечислены поименно, а не через k.*: в базах, заведенных до разделения
    # квот по моделям, у api_keys остались одноименные колонки, и они бы все затерли.
    cursor = conn.execute(
        """
        SELECT k.id AS id, k.api_key AS api_key, k.broken_reason AS broken_reason,
               ck.chat_id AS owner_chat_id, ck.position AS position,
               q.quota_date AS quota_date,
               COALESCE(q.requests_today, 0) AS requests_today,
               COALESCE(q.daily_exhausted, 0) AS daily_exhausted
        FROM api_keys k
        JOIN chat_keys ck ON ck.key_id = k.id
        LEFT JOIN key_quota q ON q.key_id = k.id AND q.model = ?
        WHERE ck.chat_id IN (?, ?)
        ORDER BY ck.chat_id = ?, ck.position, k.id
        """,
        (model, chat_id, SHARED_CHAT_ID, SHARED_CHAT_ID),
    )
    keys = db.fetch_dicts(cursor)

    # Свой ключ чата может совпасть с общим: тогда он уже в списке, и вторая строка -
    # та же самая квота под другим номером. Порядок выборки ставит свой первым.
    unique = {}
    for key in keys:
        unique.setdefault(key["id"], key)
    keys = list(unique.values())

    _reset_stale_days(conn, keys, model)
    return keys


def add_key(conn: sqlite3.Connection, chat_id: int, api_key: str) -> tuple[dict, bool]:
    """
    Привязывает ключ к чату.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param api_key: ключ Gemini
    :type api_key: str
    :return: (строка ключа, был ли он уже в пуле этого чата)
    :rtype: tuple[dict, bool]
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO api_keys (api_key, added_at) VALUES (?, ?)",
        (api_key, now),
    )
    key = db.fetch_dicts(
        conn.execute("SELECT * FROM api_keys WHERE api_key = ?", (api_key,))
    )[0]

    existing = conn.execute(
        "SELECT 1 FROM chat_keys WHERE chat_id = ? AND key_id = ?", (chat_id, key["id"])
    ).fetchone()
    if existing:
        conn.commit()
        return key, True

    position = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM chat_keys WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO chat_keys (chat_id, key_id, position, added_at) VALUES (?, ?, ?, ?)",
        (chat_id, key["id"], position, now),
    )
    conn.commit()
    logger.info("В чат %s добавлен ключ %s.", chat_id, mask_key(api_key))
    return key, False


def find_key(keys: list[dict], reference: str) -> dict | None:
    """
    Ищет ключ по номеру в списке или по концу самого ключа.

    Номер удобнее, но он живет только до следующего /keys, поэтому понимаем и хвост
    ключа - его видно в выводе /keys и он не меняется.

    :param keys: ключи чата в том же порядке, в каком их показал /keys
    :type keys: list[dict]
    :param reference: номер (с единицы) или последние символы ключа
    :type reference: str
    :return: найденный ключ или None
    :rtype: dict | None
    """
    reference = reference.strip()
    if reference.isdigit():
        index = int(reference) - 1
        return keys[index] if 0 <= index < len(keys) else None

    tail = reference.lstrip("…").lstrip(".")
    matches = [key for key in keys if key["api_key"].endswith(tail)] if tail else []
    return matches[0] if len(matches) == 1 else None


def remove_key(conn: sqlite3.Connection, chat_id: int, key: dict):
    """
    Отвязывает ключ от чата и убирает его совсем, если он больше никому не нужен.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param key: строка ключа из list_chat_keys
    :type key: dict
    """
    conn.execute(
        "DELETE FROM chat_keys WHERE chat_id = ? AND key_id = ?", (chat_id, key["id"])
    )
    left = conn.execute(
        "SELECT COUNT(*) FROM chat_keys WHERE key_id = ?", (key["id"],)
    ).fetchone()[0]
    if not left:
        # Ключ выпал из всех чатов: счетчики без него бессмысленны.
        conn.execute("DELETE FROM api_keys WHERE id = ?", (key["id"],))
        conn.execute("DELETE FROM key_quota WHERE key_id = ?", (key["id"],))
    conn.commit()
    logger.info("Из чата %s удален ключ %s.", chat_id, mask_key(key["api_key"]))


def sync_shared_key(conn: sqlite3.Connection, api_key: str | None):
    """
    Приводит общий ключ из config.cfg в соответствие с базой.

    Ключ из конфига доступен всем чатам и живет в базе на общих правах - иначе его
    дневной счетчик пришлось бы вести отдельно от остальных. Убрали ключ из конфига или
    заменили его другим - старая привязка снимается.

    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param api_key: ключ из config.cfg или None, если его там нет
    :type api_key: str | None
    """
    for key in _linked_keys(conn, SHARED_CHAT_ID):
        if key["api_key"] != api_key:
            remove_key(conn, SHARED_CHAT_ID, key)

    if api_key:
        _, already = add_key(conn, SHARED_CHAT_ID, api_key)
        if not already:
            logger.info("Общий ключ из config.cfg добавлен в базу.")


def describe_key(key: dict, index: int, active_id: int | None, daily_limit: int) -> str:
    """
    Описывает состояние ключа одной строкой для команды /keys.

    :param key: строка ключа из list_chat_keys
    :type key: dict
    :param index: номер в списке (с единицы)
    :type index: int
    :param active_id: идентификатор активного ключа чата
    :type active_id: int | None
    :param daily_limit: местный потолок запросов в сутки на ключ
    :type daily_limit: int
    :return: строка для вывода в чат
    :rtype: str
    """
    marks = []
    if key["id"] == active_id:
        marks.append("активный")
    if key["owner_chat_id"] == SHARED_CHAT_ID:
        marks.append("общий из config.cfg")

    if key["broken_reason"]:
        state = f"отклонен API: {key['broken_reason']}"
    elif key["daily_exhausted"]:
        state = f"квота на эту модель выбрана, сброс в {describe_quota_reset()}"
    else:
        limit = f" из {daily_limit}" if daily_limit else ""
        state = f"запросов сегодня: {key['requests_today']}{limit}"

    suffix = f" ({', '.join(marks)})" if marks else ""
    return f"{index}. {mask_key(key['api_key'])}{suffix} - {state}"
