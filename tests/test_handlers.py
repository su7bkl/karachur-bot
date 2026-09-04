"""
Тесты ветвей except в answer_chat: что из сбоя долетает до чата, а что оседает в логе.

Три ветки разбирают одну и ту же ошибку answer.generate_gemini_response по-разному.
key_pool.NoUsableKeys и retries.GeminiRetryError - ожидаемые исходы: пул ключей иссяк
или модель не ответила за отведенные попытки, и в обоих случаях текст исключения сам по
себе полезен человеку, поэтому уходит в чат как есть. Все остальное - неожиданность в
собственном коде бота, а не то, что говорит о самой Gemini: ее текст может нести пути
файлов и куски запроса, поэтому в чат идет только короткая фраза без подробностей, а
полный стек - в лог через logger.exception.

Сам вызов модели не подделывается сценарием FakeGemini, как в test_retries.py: тут не
важно, что происходит внутри цикла повторов, только то, как answer_chat разбирает уже
брошенное исключение. Поэтому answer.generate_gemini_response подменяется напрямую -
атрибут модуля karachur.gemini.answer, а не импортированное имя, чтобы подмена
доставала обработчик так же, как это описано в conftest.py для клиента Gemini.

Доставка ответа при этом не подделывается: deliver_response гоняется по-настоящему,
только запись в базу (save_message_to_db) подменяется по образцу test_delivery.py -
самим текстам это не мешает, а разбирать полноценный объект telegram.Message тестам
незачем.

Вторая половина файла - про живую заглушку: сюда доходит то, что слой gemini рассказал о
себе колбэком progress. Сам механизм правки разобран в test_status.py, здесь проверяется
только проводка - что колбэк вообще доехал до заглушки и что беда с ним не стоила ответа.
Этапов на такой тест хватает одного: между правками выдерживается промежуток в три
секунды, и второй этап того же ответа в чат уже не пойдет.
"""

# Подделка повторяет форму настоящего telegram.Message: отсюда parse_mode в сигнатуре,
# который самой подделке не нужен, но есть у reply_text и edit_text.
# pylint: disable=unused-argument

import asyncio
import logging
from types import SimpleNamespace

from telegram.error import TelegramError

from conftest import CHAT_ONE

from karachur.gemini import pool as key_pool
from karachur.gemini import retries, stages
from karachur.tg import delivery, handlers


class _FakeMessage:
    """
    Подделка telegram.Message: помнит все тексты, что через нее ушли в чат.

    reply_text заводит плейсхолдер (и им же сама себя подменяет - тот же объект годится
    для правки), а edit_text заменяет его текст на итоговый ответ. Оба метода async, как
    настоящие, и оба складывают текст в self.sent по порядку - assert смотрит на
    последний, готовый ответ.
    """

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.message_id = 1
        self.sent: list[str] = []

    async def reply_text(self, text, parse_mode=None):
        """Как настоящий reply_text: запоминает текст и возвращает себя же."""
        self.sent.append(text)
        return self

    async def edit_text(self, text, parse_mode=None):
        """Как настоящий edit_text заглушки: запоминает итоговый текст."""
        self.sent.append(text)
        return self


def _run_answer_chat(cfg, db, monkeypatch, raise_exc):
    """
    Гоняет answer_chat с подмененным generate_gemini_response, который бросает raise_exc.

    :param raise_exc: исключение, которым подмена отвечает на любой вызов
    :return: (сообщение с записанными текстами, список аргументов save_message_to_db)
    :rtype: tuple[_FakeMessage, list[tuple[bool, str | None]]]
    """

    async def _fake_generate(*_args, **_kwargs):
        raise raise_exc

    monkeypatch.setattr(handlers.answer, "generate_gemini_response", _fake_generate)

    save_calls = []

    def _fake_save(_conn, _message, is_bot=False, content_override=None):
        """Вместо записи в базу запоминает, чем и с каким флагом ее звали."""
        save_calls.append((is_bot, content_override))
        return (None, None, None)

    monkeypatch.setattr(handlers.messages, "save_message_to_db", _fake_save)

    message = _FakeMessage(CHAT_ONE)
    context = SimpleNamespace(bot_data={"db_conn": db})
    asyncio.run(handlers.answer_chat(cfg, context, message, transcribe_only=False))
    return message, save_calls


def test_gemini_retry_error_shown_in_chat_as_is(cfg, db, monkeypatch):
    """
    GeminiRetryError - ожидаемый исход: текст исключения уходит в чат целиком.

    Он объясняет, сколько попыток сделано и почему сдались, и это ровно то, что нужно
    человеку - подробность здесь не мешает, а помогает.
    """
    exc = retries.GeminiRetryError(
        "исчерпаны все 4 попытки: 429 RESOURCE_EXHAUSTED, повтор бесполезен"
    )

    message, save_calls = _run_answer_chat(cfg, db, monkeypatch, exc)

    assert str(exc) in message.sent[-1]
    assert message.sent[-1] == f"Произошла ошибка при обращении к нейросети: {exc}"
    # err=True: в контекст модели вместо простыни с ошибкой уходит короткая пометка.
    assert save_calls == [(True, delivery.ERROR_CONTEXT_NOTE)]


def test_no_usable_keys_still_shown_in_chat_as_is(cfg, db, monkeypatch):
    """
    Ветка NoUsableKeys не тронута: пул ключей иссяк - это тоже не поломка бота, а
    рабочая ситуация, где человеку нужен именно текст с инструкцией, что делать.

    Регрессия на случай, если бы GeminiRetryError или общий except перехватили эту
    ошибку раньше своей ветки.
    """
    exc = key_pool.NoUsableKeys("нет ни одного рабочего ключа, добавьте новый /addkey")

    message, save_calls = _run_answer_chat(cfg, db, monkeypatch, exc)

    assert message.sent[-1] == f"Не могу ответить: {exc}"
    assert save_calls == [(True, delivery.ERROR_CONTEXT_NOTE)]


def test_unexpected_error_does_not_leak_into_chat(cfg, db, monkeypatch):
    """
    Неожиданная ошибка (не GeminiRetryError и не NoUsableKeys) - в чат идет короткая
    фраза без текста исключения. Внутренности вроде пути к файлу или куска запроса
    участникам чата видеть не нужно, а ошибка нашего кода не должна выглядеть так, будто
    виновата нейросеть.
    """
    secret = "TypeError: /home/bot/media/секретный_файл.ogg not subscriptable"
    exc = TypeError(secret)

    message, save_calls = _run_answer_chat(cfg, db, monkeypatch, exc)

    final_text = message.sent[-1]
    assert secret not in final_text
    assert "нейросети" not in final_text
    assert "внутренняя ошибка" in final_text.lower()
    assert save_calls == [(True, delivery.ERROR_CONTEXT_NOTE)]


def test_unexpected_error_full_text_goes_to_log(cfg, db, monkeypatch, caplog):
    """
    Полный текст неожиданной ошибки не пропадает - он уходит в лог через
    logger.exception, со стеком, а не одной строкой через logger.error.

    caplog проверяет это по exc_info записи: он появляется только тогда, когда логер
    зовут внутри except с намерением приложить трейсбек (logger.exception или
    logger.error(..., exc_info=True)), а не при обычном логировании одной строкой.
    """
    secret = "AttributeError: у объекта нет поля bar (после правки contents.py)"
    exc = AttributeError(secret)

    with caplog.at_level(logging.ERROR, logger="karachur.tg.handlers"):
        message, _ = _run_answer_chat(cfg, db, monkeypatch, exc)

    assert secret not in message.sent[-1]

    exception_records = [r for r in caplog.records if r.exc_info]
    assert exception_records, "стек ошибки не попал в лог через logger.exception"
    assert str(exception_records[0].exc_info[1]) == secret


class _UneditableMessage(_FakeMessage):
    """Сообщение, у которого Telegram отбивает любую правку."""

    def __init__(self, chat_id: int):
        super().__init__(chat_id)
        self.deleted = False

    async def edit_text(self, text, parse_mode=None):
        """Как настоящий edit_text на удаленном сообщении - отказом."""
        raise TelegramError("сообщение для правки не найдено")

    async def delete(self):
        """Заглушку, которую не вышло исправить, доставка убирает из чата."""
        self.deleted = True


def _run_with_stages(cfg, db, monkeypatch, message, *reported, transcribe_only=False):
    """
    Гоняет answer_chat с подменой, которая по дороге сообщает об этапах.

    :param message: подделка сообщения, на которое отвечаем
    :param reported: этапы, о которых подмена расскажет через progress
    :param transcribe_only: гоним ли ветку расшифровки голосового
    """

    async def _fake_generate(*_args, progress=None, **_kwargs):
        """Вместо разговора с моделью рассказывает об этапах и отдает готовый текст."""
        for stage in reported:
            await stages.report(progress, stage)
        return "готовый ответ"

    monkeypatch.setattr(handlers.answer, "generate_gemini_response", _fake_generate)
    monkeypatch.setattr(
        handlers.messages, "save_message_to_db", lambda *a, **k: (None, None, None)
    )

    context = SimpleNamespace(bot_data={"db_conn": db})
    asyncio.run(handlers.answer_chat(cfg, context, message, transcribe_only))


def test_stage_reaches_the_placeholder(cfg, db, monkeypatch):
    """
    Этап, о котором сообщил слой gemini, виден в заглушке до прихода ответа.

    Ради этого все и затевалось: между заглушкой и ответом могут пройти минуты, и все
    это время человеку нужно видеть, что бот занят делом, а не завис.
    """
    message = _FakeMessage(CHAT_ONE)

    _run_with_stages(cfg, db, monkeypatch, message, stages.Stage(stages.CONTEXT))

    assert message.sent == [
        delivery.GENERATING_PLACEHOLDER,
        "⏳ Собираю контекст чата...",
        "готовый ответ",
    ]


def test_transcription_says_so_in_the_placeholder(cfg, db, monkeypatch):
    """
    Расшифровка голосового называет себя сама: слой gemini о ней знать не может.

    Для него это такой же запрос, как всякий другой, и отличить его можно только здесь,
    по transcribe_only.
    """
    message = _FakeMessage(CHAT_ONE)

    _run_with_stages(cfg, db, monkeypatch, message, transcribe_only=True)

    assert message.sent == [
        delivery.GENERATING_PLACEHOLDER,
        "⏳ Расшифровываю голосовое...",
        "готовый ответ",
    ]


def test_broken_status_does_not_cost_the_answer(cfg, db, monkeypatch):
    """
    Заглушку, которую нельзя править, статус переживает молча - ответ доходит до чата.

    Человеку нужен ответ, а не красивый статус: если заглушку удалили или чат закрыли,
    доставка убирает ее и отвечает обычным сообщением, как и без всякого статуса.
    """
    message = _UneditableMessage(CHAT_ONE)

    _run_with_stages(cfg, db, monkeypatch, message, stages.Stage(stages.ASKING))

    assert message.sent[-1] == "готовый ответ"
    assert message.deleted
