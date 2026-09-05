"""
Пул ключей Gemini: ключи принадлежат чату, бот сам переключается между ними.

Хранение ключей - таблицы, привязка к чатам, дневные счетчики - лежит в
karachur.storage.keys, а разбор ошибок API - в karachur.gemini.errors. Здесь остается
поведение: кем из ключей идти в API сейчас и когда пора уходить на следующий.

Ключ выбывает из работы по двум причинам. Дневная квота (RPD) выбрана - до полуночи по
тихоокеанскому времени, когда Google обнуляет счетчики. Ключ отвергнут API - навсегда,
пока его не удалят и не добавят заново: сам собой такой ключ не починится.

Отвергнутым ключ считается только по опознанной причине: невалидный или отозванный ключ,
приостановленный проект, ограничение по адресу, невключенный Generative Language API.
Неопознанный 403 ключа не касается - что именно за ним стоит, решает karachur.gemini.errors.
"""

import logging
import sqlite3

from google import genai

# Модуль хранения по всему коду зовется key_store, а не keys: в пуле и в командах "keys"
# уже занято локальными списками ключей, и модуль под тем же именем ими бы перекрывался.
from karachur.storage import keys as key_store
from karachur.storage import settings

logger = logging.getLogger(__name__)

# Клиент - тонкая обертка над ключом, но плодить их на каждый запрос незачем.
_CLIENTS: dict[str, genai.Client] = {}


class NoUsableKeys(Exception):
    """В чате не осталось ключа, которым можно сходить в Gemini."""


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
        return key_store.list_chat_keys(self.conn, self.chat_id, self.model)

    def live_keys(self) -> list[dict]:
        """
        Возвращает ключи чата, которых API еще не отверг, не глядя на квоты.

        Дневная квота выбирается на пару "ключ и модель", поэтому для дела, к модели
        отношения не имеющего - того же списка моделей, - выбранная квота не значит
        ничего. Такой ключ и нужен, чтобы чат не запирало: на исчерпанной модели
        active() не отдает никого, а список моделей человеку нужен ровно в этот
        момент - иначе уйти на модель со своей квотой можно только вслепую.

        :return: строки ключей в порядке обхода
        :rtype: list[dict]
        """
        return [key for key in self.keys() if not key["broken_reason"]]

    def active_key_id(self) -> int | None:
        """
        Возвращает идентификатор ключа, на котором чат остановился.

        :return: идентификатор ключа или None
        :rtype: int | None
        """
        return settings.get_active_key_id(self.conn, self.chat_id)

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
            settings.set_active_key_id(self.conn, self.chat_id, key["id"])
            logger.info(
                "Чат %s переключился на ключ %s.",
                self.chat_id,
                key_store.mask_key(key["api_key"]),
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
                            key_store.mask_key(key["api_key"]),
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
                key_store.mask_key(key["api_key"]),
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
        today = key_store.quota_date()
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
        today = key_store.quota_date()
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
            key_store.mask_key(key["api_key"]),
            self.model,
            key_store.describe_quota_reset(),
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
        logger.error(
            "Ключ %s отвергнут API: %s", key_store.mask_key(key["api_key"]), short
        )

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
                f"Квоты на эту модель обнулятся в {key_store.describe_quota_reset()}, "
                "но у других моделей квота своя: посмотрите /model. "
            )
        return message + "Состояние ключей покажет /keys"
