"""
Пул ключей Gemini: ключи принадлежат чату, бот сам переключается между ними.

Сам ключ хранится один раз на всю базу (api_keys), а привязка к чатам лежит отдельно
(chat_keys). Так сделано потому, что дневную квоту Google считает на ключ, а не на чат:
добавь один и тот же ключ в два чата - и счетчик у него все равно должен быть общий,
иначе бот будет думать, что квоты вдвое больше, чем есть.

Ключ выбывает из работы по двум причинам. Дневная квота (RPD) выбрана - до полуночи по
тихоокеанскому времени, когда Google обнуляет счетчики. Ключ отвергнут API (401, 403,
"API key not valid") - навсегда, пока его не удалят и не добавят заново: сам собой такой
ключ не починится.
"""

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google import genai

import chat_settings

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

# --- РАЗБОР ОШИБОК API ---
# Что именно случилось, решает судьбу ключа, поэтому разбор живет рядом с пулом.
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

# Клиент - тонкая обертка над ключом, но плодить их на каждый запрос незачем.
_CLIENTS: dict[str, genai.Client] = {}


class NoUsableKeys(Exception):
    """В чате не осталось ключа, которым можно сходить в Gemini."""


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


def client_for_key(api_key: str) -> genai.Client:
    """
    Возвращает клиента Gemini для конкретного ключа.

    :param api_key: ключ Gemini
    :type api_key: str
    :return: клиент, работающий от этого ключа
    :rtype: genai.Client
    """
    client = _CLIENTS.get(api_key)
    if client is None:
        client = genai.Client(api_key=api_key)
        _CLIENTS[api_key] = client
    return client


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


def _fetch_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """
    Превращает результат запроса в список словарей.

    :param cursor: курсор с выполненным запросом
    :type cursor: sqlite3.Cursor
    :return: строки результата
    :rtype: list[dict]
    """
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


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
    return _fetch_dicts(
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
    keys = _fetch_dicts(cursor)

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
    key = _fetch_dicts(
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


class KeyPool:
    """
    Ключи одного чата под одну модель и переключение между ними.

    Пул не держит состояние в себе: и счетчики, и указатель на активный ключ лежат в
    базе. Так ротация переживает перезапуск бота, а два чата с одним и тем же ключом
    видят один общий счетчик.

    Пул всегда привязан к модели: дневная квота у Gemini своя на каждую пару "проект и
    модель", поэтому выбранная квота одной модели ничего не говорит об остальных.
    """

    def __init__(
        self, conn: sqlite3.Connection, chat_id: int, model: str, daily_limit: int
    ):
        """
        :param conn: соединение с базой данных
        :type conn: sqlite3.Connection
        :param chat_id: идентификатор чата
        :type chat_id: int
        :param model: модель, для которой нужен ключ
        :type model: str
        :param daily_limit: местный потолок запросов в сутки на ключ (0 - не считать)
        :type daily_limit: int
        """
        self.conn = conn
        self.chat_id = chat_id
        self.model = model
        self.daily_limit = daily_limit

    def keys(self) -> list[dict]:
        """
        Возвращает ключи, доступные чату, со счетчиками по модели пула.

        :return: строки ключей в порядке обхода
        :rtype: list[dict]
        """
        return list_chat_keys(self.conn, self.chat_id, self.model)

    def active_key_id(self) -> int | None:
        """
        Возвращает идентификатор ключа, на котором чат остановился.

        :return: идентификатор ключа или None
        :rtype: int | None
        """
        return chat_settings.get_active_key_id(self.conn, self.chat_id)

    def is_usable(self, key: dict, ignore_local_limit: bool = False) -> bool:
        """
        Решает, можно ли идти в API этим ключом.

        :param key: строка ключа
        :type key: dict
        :param ignore_local_limit: не смотреть на собственный счетчик запросов
        :type ignore_local_limit: bool
        :return: True, если ключ пригоден
        :rtype: bool
        """
        if key["broken_reason"] or key["daily_exhausted"]:
            return False
        if ignore_local_limit or not self.daily_limit:
            return True
        return key["requests_today"] < self.daily_limit

    @staticmethod
    def _ring(keys: list[dict], active_id: int | None) -> list[dict]:
        """
        Переставляет список так, чтобы обход начинался с активного ключа.

        :param keys: ключи чата
        :type keys: list[dict]
        :param active_id: идентификатор активного ключа
        :type active_id: int | None
        :return: тот же список, прокрученный до активного ключа
        :rtype: list[dict]
        """
        start = next((i for i, key in enumerate(keys) if key["id"] == active_id), 0)
        return keys[start:] + keys[:start]

    def _remember(self, key: dict):
        """
        Запоминает ключ как активный, если он таким еще не был.

        :param key: строка ключа
        :type key: dict
        """
        if self.active_key_id() != key["id"]:
            chat_settings.set_active_key_id(self.conn, self.chat_id, key["id"])
            logger.info(
                "Чат %s переключился на ключ %s.", self.chat_id, mask_key(key["api_key"])
            )

    def _pick(self, candidates: list[dict]) -> dict | None:
        """
        Выбирает первый пригодный ключ из предложенных.

        Местный счетчик запросов - страховка, а не закон: дневной лимит бесплатного
        тарифа зависит от модели, и настройка легко оказывается заниженной. Поэтому
        сначала обходим кандидатов по счетчику, а если по нему не годится никто -
        пробуем еще раз, не глядя на него: пусть лучше откажет API, чем бот сам себе
        запретит работать при живой квоте.

        :param candidates: ключи в порядке обхода
        :type candidates: list[dict]
        :return: пригодный ключ или None, если таких нет
        :rtype: dict | None
        """
        for ignore_local_limit in (False, True):
            for key in candidates:
                if self.is_usable(key, ignore_local_limit):
                    if ignore_local_limit:
                        logger.warning(
                            "Все ключи чата %s выбрали местный лимит %d, "
                            "пробуем %s сверх него.",
                            self.chat_id,
                            self.daily_limit,
                            mask_key(key["api_key"]),
                        )
                    self._remember(key)
                    return key
        return None

    def active(self) -> dict:
        """
        Возвращает ключ, которым идем в API сейчас.

        :return: строка ключа
        :rtype: dict
        :raises NoUsableKeys: если пул пуст или все ключи выбыли
        """
        keys = self.keys()
        if not keys:
            raise NoUsableKeys(
                "у чата нет ни одного ключа Gemini. Добавьте его командой /addkey"
            )

        key = self._pick(self._ring(keys, self.active_key_id()))
        if key is None:
            raise NoUsableKeys(self._describe_dead_pool(keys))
        return key

    def rotate(self, reason: str) -> dict | None:
        """
        Переключает чат на следующий пригодный ключ.

        :param reason: с чем связана ротация - уходит в лог
        :type reason: str
        :return: новый активный ключ или None, если менять не на что
        :rtype: dict | None
        """
        keys = self.keys()
        if len(keys) < 2:
            return None

        # Текущий ключ пропускаем: смысл ротации в том, чтобы уйти именно с него.
        key = self._pick(self._ring(keys, self.active_key_id())[1:])
        if key is not None:
            logger.info(
                "Чат %s ротировал ключ (%s) на %s.",
                self.chat_id,
                reason,
                mask_key(key["api_key"]),
            )
        return key

    def client(self, key: dict) -> genai.Client:
        """
        Возвращает клиента Gemini для этого ключа.

        :param key: строка ключа
        :type key: dict
        :return: клиент
        :rtype: genai.Client
        """
        return client_for_key(key["api_key"])

    def note_request(self, key: dict):
        """
        Отмечает потраченный запрос в счетчике этой пары "ключ и модель".

        :param key: строка ключа
        :type key: dict
        """
        today = quota_date()
        self.conn.execute(
            """
            INSERT INTO key_quota (key_id, model, quota_date, requests_today)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(key_id, model) DO UPDATE SET
                requests_today = CASE
                    WHEN key_quota.quota_date = excluded.quota_date
                    THEN key_quota.requests_today + 1 ELSE 1 END,
                quota_date = excluded.quota_date
            """,
            (key["id"], self.model, today),
        )
        self.conn.commit()
        key["requests_today"] = key.get("requests_today", 0) + 1
        key["quota_date"] = today

    def mark_daily_exhausted(self, key: dict):
        """
        Помечает, что у ключа кончилась дневная квота на модель пула.

        Помечается именно пара "ключ и модель": квота у Gemini своя на каждую модель, и
        выбранная квота одной из них не делает ключ негодным для остальных. Иначе один
        запрос к модели без бесплатной квоты выводил бы из строя весь пул чата до
        полуночи.

        :param key: строка ключа
        :type key: dict
        """
        today = quota_date()
        self.conn.execute(
            """
            INSERT INTO key_quota (key_id, model, quota_date, daily_exhausted)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(key_id, model) DO UPDATE SET
                daily_exhausted = 1, quota_date = excluded.quota_date
            """,
            (key["id"], self.model, today),
        )
        self.conn.commit()
        key["daily_exhausted"] = 1
        key["quota_date"] = today
        logger.warning(
            "У ключа %s кончилась дневная квота на модель %s, сброс в %s.",
            mask_key(key["api_key"]),
            self.model,
            describe_quota_reset(),
        )

    def mark_broken(self, key: dict, reason: str):
        """
        Помечает ключ как отвергнутый API: сам он больше не заработает.

        :param key: строка ключа
        :type key: dict
        :param reason: текст ошибки от API
        :type reason: str
        """
        # Полная ошибка бывает на десяток строк, а в /keys нужна одна.
        short = " ".join(str(reason).split())[:200]
        self.conn.execute(
            "UPDATE api_keys SET broken_reason = ? WHERE id = ?", (short, key["id"])
        )
        self.conn.commit()
        key["broken_reason"] = short
        logger.error("Ключ %s отвергнут API: %s", mask_key(key["api_key"]), short)


    def _describe_dead_pool(self, keys: list[dict]) -> str:
        """
        Объясняет, почему ни один ключ чата не годится.

        :param keys: ключи чата
        :type keys: list[dict]
        :return: текст для чата и лога
        :rtype: str
        """
        broken = sum(1 for key in keys if key["broken_reason"])
        exhausted = sum(1 for key in keys if key["daily_exhausted"])
        parts = []
        if exhausted:
            parts.append(f"у {exhausted} кончилась дневная квота на эту модель")
        if broken:
            parts.append(f"{broken} отклонены API")
        details = ", ".join(parts) if parts else "все непригодны"
        message = (
            f"ни один из {len(keys)} ключей чата не работает с моделью "
            f"{self.model} ({details}). "
        )
        if exhausted:
            # Квота считается на каждую модель отдельно, так что дело может быть не в
            # ключах, а в модели - у части моделей бесплатной квоты нет вовсе.
            message += (
                f"Квоты на эту модель обнулятся в {describe_quota_reset()}, но у других "
                "моделей квота своя: посмотрите /model. "
            )
        return message + "Состояние ключей покажет /keys"
