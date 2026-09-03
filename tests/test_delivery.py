"""
Тесты доставки ответа в чат: пауза между кусками длинного сообщения.

Раньше пауза между кусками делалась синхронным time.sleep(10) внутри async-функции -
это морозило весь event loop на десять секунд, то есть все чаты бота разом, а не только
тот, что дожидается своих кусков. Плюс пауза ставилась и после последнего куска, хотя
ждать там уже нечего. Тесты здесь проверяют исправленное поведение: пауза - это
await asyncio.sleep, ее нет после последнего куска, и она вообще не заводится, если
кусков четыре или меньше.

Разбивка на куски (split_html_message) в тестах подменяется заранее заданным списком:
самим тестам неинтересно, как HTML режется на части, только сколько раз и как ставится
пауза между уже готовыми кусками. Запись в базу (save_message_to_db) тоже подменяется -
форму сохраненной строки проверяют тесты karachur.storage.messages, а не эти.

Телеграм-сообщение подделано минимально, по образцу conftest.py: deliver_response в этих
тестах ходит только в reply_text (плейсхолдер всегда отсутствует, поэтому
replace_placeholder уходит в ветку original.reply_text), а возвращенный объект сам себя же
и подставляет - его содержимое save_message_to_db все равно не смотрит, она подменена.
"""

# Подделка повторяет форму настоящего telegram.Message: отсюда класс с одним методом и
# аргументы, которые тесту не нужны, но есть в исходной сигнатуре.
# pylint: disable=too-few-public-methods,unused-argument

import asyncio
import time

import pytest

from karachur.tg import delivery


class _FakeMessage:
    """Подделка telegram.Message с одним методом, который в деле и используется."""

    async def reply_text(self, text, parse_mode=None):
        """Как настоящий reply_text: async и с тем же готовым результатом - самим собой."""
        return self


@pytest.fixture(autouse=True)
def _no_db_writes(monkeypatch):
    """
    Отключает запись в базу на время этих тестов.

    deliver_response кладет каждый кусок ответа в базу через
    karachur.storage.messages.save_message_to_db, а той нужен настоящий объект
    telegram.Message нужной формы (message.date, message.from_user и т.д.) - здесь это
    лишняя забота, тестам интересна только пауза между кусками.
    """
    monkeypatch.setattr(delivery.messages, "save_message_to_db", lambda *a, **k: None)


@pytest.fixture(name="no_real_sleep", autouse=True)
def no_real_sleep_fixture(monkeypatch):
    """
    Ловит настоящий time.sleep - если бы дефект вернулся, тест не завис бы на десять
    секунд, а сразу упал с понятной причиной.
    """

    def _forbidden(*_a, **_k):
        raise AssertionError("time.sleep не должен вызываться из deliver_response")

    monkeypatch.setattr(time, "sleep", _forbidden)


def _with_fixed_chunks(monkeypatch, chunks):
    """Подменяет split_html_message заранее заданным списком кусков."""
    monkeypatch.setattr(delivery, "split_html_message", lambda html, max_chars: chunks)


def _patch_asyncio_sleep(monkeypatch):
    """Подменяет asyncio.sleep записью вызовов вместо настоящего ожидания."""
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return calls


def test_pause_between_more_than_four_chunks(monkeypatch):
    """
    При пяти кусках пауза ставится после каждого, кроме последнего - то есть четыре раза,
    а не пять: ждать после последнего куска уже нечего.
    """
    chunks = [f"кусок {i}" for i in range(5)]
    _with_fixed_chunks(monkeypatch, chunks)
    sleeps = _patch_asyncio_sleep(monkeypatch)

    asyncio.run(
        delivery.deliver_response(None, _FakeMessage(), None, "неважно что тут", False)
    )

    assert sleeps == [delivery.CHUNK_PAUSE] * (len(chunks) - 1)


def test_no_pause_for_four_or_fewer_chunks(monkeypatch):
    """Ответ из четырех кусков (и короче) уходит в чат вовсе без пауз."""
    chunks = [f"кусок {i}" for i in range(4)]
    _with_fixed_chunks(monkeypatch, chunks)
    sleeps = _patch_asyncio_sleep(monkeypatch)

    asyncio.run(
        delivery.deliver_response(None, _FakeMessage(), None, "неважно что тут", False)
    )

    assert not sleeps
