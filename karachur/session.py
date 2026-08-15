"""
Данные одного обращения к чату, собранные в одной точке.

Ответ на сообщение чата всегда собирается из одного и того же набора: настройки бота,
соединение с базой, идентификатор чата и пул ключей Gemini под текущую модель этого
чата. Раньше пул собирался в двух местах порознь одним и тем же кодом (commands._pool и
участок в bot.answer_chat) - и при первой же правке сборки копии грозили разъехаться,
потому что менять пришлось бы оба места сразу, а про второе легко забыть.

ChatSession.create - единственная точка, где эти четыре значения сходятся вместе. Сам
объект дальше идет по цепочке обработки ответа одним аргументом вместо четырех
поштучных.
"""

import sqlite3
from dataclasses import dataclass

import api_keys
from karachur import config
from karachur.storage import settings


@dataclass(frozen=True)
class ChatSession:
    """
    Снимок того, с чем чат идет отвечать: настройки, база, сам чат и его пул ключей.

    Замороженный по той же причине, что и Config: собирается один раз на вход в
    обработку сообщения и дальше только читается, а не правится по ходу дела.
    """

    cfg: config.Config
    conn: sqlite3.Connection
    chat_id: int
    pool: api_keys.KeyPool

    @classmethod
    def create(
        cls, cfg: config.Config, conn: sqlite3.Connection, chat_id: int
    ) -> "ChatSession":
        """
        Собирает сессию чата: спрашивает модель у настроек и строит под нее пул ключей.

        Модель нужна пулу не для самого запроса, а для счетчиков: дневная квота у
        Gemini своя на каждую модель, и состояние ключа имеет смысл только вместе с ней.

        :param cfg: настройки бота
        :type cfg: config.Config
        :param conn: соединение с базой данных
        :type conn: sqlite3.Connection
        :param chat_id: идентификатор чата
        :type chat_id: int
        :return: собранная сессия чата
        :rtype: ChatSession
        """
        model = settings.get_model(conn, chat_id, cfg.model)
        pool = api_keys.KeyPool(conn, chat_id, model, cfg.key_rpd_limit)
        return cls(cfg=cfg, conn=conn, chat_id=chat_id, pool=pool)
