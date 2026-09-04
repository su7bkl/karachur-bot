"""
Тесты живой заглушки: что человек видит, пока бот собирает ответ.

Проверяется тут не столько текст, сколько три обещания, без которых живая заглушка
вредна. Первое: этапы доходят до чата по порядку. Второе: частые этапы не превращаются в
частые правки - Telegram отбивает их по лимиту, а на упорство отвечает временной
блокировкой бота в чате, и тогда не доедет уже и сам ответ. Третье: беда со статусом
остается бедой статуса - заглушку могли удалить, чат закрыть, правку отбить, и ни одно из
этого не должно вылезти наружу исключением.

Время тут не идет само: часы (status.now) подменяются, и тест двигает их руками. Иначе
проверка промежутка между правками означала бы настоящее ожидание в три секунды на каждый
случай.

Подделка заглушки ведет два журнала, и разница между ними значащая: attempts - все
попытки правки, texts - только доехавшие. По первому видно, что запрос к Telegram вообще
не отправлялся (повтор того же текста), по второму - что он не доехал (ошибка).
"""

# Подделка повторяет форму настоящего telegram.Message: отсюда класс с одним методом и
# аргумент parse_mode, который тесту не нужен, но есть у edit_text.
# pylint: disable=too-few-public-methods,unused-argument

import asyncio
import logging

import pytest
from telegram.error import TelegramError

from karachur.gemini import errors, stages
from karachur.tg import delivery, status


class _FakePlaceholder:
    """Подделка заглушки: помнит и попытки правки, и то, что из них вышло."""

    def __init__(self, fail: bool = False):
        """
        :param fail: True - Telegram отбивает любую правку
        :type fail: bool
        """
        self.fail = fail
        self.attempts: list[str] = []
        self.texts: list[str] = []

    async def edit_text(self, text, parse_mode=None):
        """Как настоящий edit_text: async, с тем же результатом - самим сообщением."""
        self.attempts.append(text)
        if self.fail:
            raise TelegramError("сообщение для правки не найдено")
        self.texts.append(text)
        return self


def fake_clock(monkeypatch):
    """
    Подменяет часы статуса и отдает ручку, которой тест двигает время.

    :param monkeypatch: штатная подмена атрибутов pytest
    :return: функция, прибавляющая к часам заданное число секунд
    """
    moment = {"now": 1000.0}
    monkeypatch.setattr(status, "now", lambda: moment["now"])

    def tick(seconds: float):
        """Двигает часы вперед."""
        moment["now"] += seconds

    return tick


def drive(live, tick, *steps):
    """
    Скармливает статусу этапы, двигая между ними часы.

    :param live: объект статуса
    :type live: status.StatusMessage
    :param tick: ручка часов от fake_clock
    :param steps: пары (этап, на сколько секунд сдвинуть часы после него)
    """

    async def _run():
        """Гоняет шаги по порядку - статус асинхронный, как и его получатель."""
        for stage, pause in steps:
            await live(stage)
            tick(pause)

    asyncio.run(_run())


def test_stages_reach_the_placeholder_in_order(monkeypatch):
    """Этапы доходят до заглушки по очереди и теми словами, которые задуманы."""
    tick = fake_clock(monkeypatch)
    placeholder = _FakePlaceholder()
    live = status.StatusMessage(placeholder)

    drive(
        live,
        tick,
        (stages.Stage(stages.CONTEXT), status.EDIT_INTERVAL),
        (stages.Stage(stages.ATTACHMENTS), status.EDIT_INTERVAL),
        (stages.Stage(stages.COMPRESS, 1, 3), status.EDIT_INTERVAL),
        (stages.Stage(stages.ASKING), status.EDIT_INTERVAL),
    )

    assert placeholder.texts == [
        "⏳ Собираю контекст чата...",
        "⏳ Готовлю вложения...",
        "⏳ Сжимаю историю чата (проход 1 из 3)...",
        "⏳ Спрашиваю модель...",
    ]


def test_frequent_stages_do_not_become_frequent_edits(monkeypatch):
    """
    Этапы, пришедшие подряд, укладываются в одну правку, а не в четыре.

    Сборка запроса и начало попытки идут очередями по несколько этапов за доли секунды.
    Если каждую такую очередь честно выписывать в чат, Telegram отобьет правки по лимиту,
    а бота в чате придержит - вместе с ответом, ради которого все и затевалось.
    """
    tick = fake_clock(monkeypatch)
    placeholder = _FakePlaceholder()
    live = status.StatusMessage(placeholder)

    drive(
        live,
        tick,
        # Первая правка идет сразу: заглушка только что отправлена, ждать нечего.
        (stages.Stage(stages.CONTEXT), 0.5),
        (stages.Stage(stages.ATTACHMENTS), 0.5),
        (stages.Stage(stages.MEASURING), 0.5),
        (stages.Stage(stages.ASKING), status.EDIT_INTERVAL),
    )

    assert placeholder.attempts == ["⏳ Собираю контекст чата..."]


def test_skipped_stage_is_not_lost_forever(monkeypatch):
    """
    Пропущенный по промежутку этап не оставляет заглушку с устаревшим текстом.

    Ближайшая разрешенная правка показывает текущее положение дел, а не очередь того,
    что бот успел пережить, пока молчал.
    """
    tick = fake_clock(monkeypatch)
    placeholder = _FakePlaceholder()
    live = status.StatusMessage(placeholder)

    drive(
        live,
        tick,
        (stages.Stage(stages.CONTEXT), 0.1),
        (stages.Stage(stages.ATTACHMENTS), 0.1),
        (stages.Stage(stages.MEASURING), status.EDIT_INTERVAL),
        (
            stages.Stage(stages.RETRY, 2, 15, 30.0, errors.ERROR_KIND_RATE),
            status.EDIT_INTERVAL,
        ),
    )

    # Ни "готовлю вложения", ни "считаю размер" - в чате то, что происходит сейчас.
    assert placeholder.texts == [
        "⏳ Собираю контекст чата...",
        "⏳ Модель занята. Попытка 2 из 15 через 30 с...",
    ]


def test_same_text_is_not_sent_twice(monkeypatch):
    """
    Повторный этап с тем же текстом до Telegram не доходит вовсе.

    На правку, которая ничего не меняет, Telegram отвечает ошибкой "message is not
    modified", а этапы повторяются постоянно: каждая попытка запроса начинается с того
    же "спрашиваю модель".
    """
    tick = fake_clock(monkeypatch)
    placeholder = _FakePlaceholder()
    live = status.StatusMessage(placeholder)

    drive(
        live,
        tick,
        (stages.Stage(stages.ASKING), status.EDIT_INTERVAL),
        (stages.Stage(stages.ASKING), status.EDIT_INTERVAL),
        (stages.Stage(stages.ASKING), status.EDIT_INTERVAL),
    )

    assert placeholder.attempts == ["⏳ Спрашиваю модель..."]


def test_unknown_stage_leaves_the_usual_placeholder(monkeypatch):
    """Незнакомый этап не превращается в ерунду в чате: остается обычная заглушка."""
    tick = fake_clock(monkeypatch)
    placeholder = _FakePlaceholder()
    live = status.StatusMessage(placeholder)

    drive(live, tick, (stages.Stage("этап из будущего"), status.EDIT_INTERVAL))

    assert status.render_stage(stages.Stage("этап из будущего")) == (
        delivery.GENERATING_PLACEHOLDER
    )
    # Текст заглушки не изменился - значит, и правку слать было незачем.
    assert not placeholder.attempts


def test_broken_edit_does_not_escape(monkeypatch, caplog):
    """
    Отбитая правка статуса не рвется наружу исключением и не хоронит статус насовсем.

    Заглушку могли удалить, чат закрыть, лимит сработать - все это мелочь рядом с
    ответом. Беда уходит в лог, а следующий этап пробует снова: причина могла и пройти.
    """
    tick = fake_clock(monkeypatch)
    placeholder = _FakePlaceholder(fail=True)
    live = status.StatusMessage(placeholder)

    with caplog.at_level(logging.WARNING, logger="karachur.tg.status"):
        drive(
            live,
            tick,
            (stages.Stage(stages.CONTEXT), status.EDIT_INTERVAL),
            (stages.Stage(stages.ASKING), status.EDIT_INTERVAL),
        )

    assert placeholder.attempts == [
        "⏳ Собираю контекст чата...",
        "⏳ Спрашиваю модель...",
    ]
    assert not placeholder.texts
    complaints = [r for r in caplog.records if r.name == "karachur.tg.status"]
    assert len(complaints) == 2


def test_missing_placeholder_is_harmless(monkeypatch):
    """
    Заглушки может не быть вовсе - send_placeholder возвращает None, если не отправилась.

    Тогда статус молча ничего не делает, и звать его все равно можно: обработчику
    незачем оглядываться на то, дошла заглушка до чата или нет.
    """
    tick = fake_clock(monkeypatch)
    live = status.StatusMessage(None)

    drive(
        live,
        tick,
        (stages.Stage(stages.CONTEXT), status.EDIT_INTERVAL),
        (stages.Stage(stages.ASKING), status.EDIT_INTERVAL),
    )

    assert live.pending == "⏳ Спрашиваю модель..."


@pytest.mark.parametrize(
    "error_kind, expected",
    [
        (errors.ERROR_KIND_RATE, "⏳ Модель занята. Попытка 3 из 15 через 12 с..."),
        (
            errors.ERROR_KIND_DAILY,
            "⏳ Ключ выдохся, беру следующий. Попытка 3 из 15 через 12 с...",
        ),
        (
            errors.ERROR_KIND_TRANSIENT,
            "⏳ Временный сбой. Попытка 3 из 15 через 12 с...",
        ),
        (None, "⏳ Модель не ответила. Попытка 3 из 15 через 12 с..."),
    ],
)
def test_retry_reason_is_written_in_human_words(error_kind, expected):
    """Причину повтора человек читает словами, а не кодом ошибки из ответа API."""
    stage = stages.Stage(stages.RETRY, 3, 15, 12.0, error_kind)

    assert status.render_stage(stage) == expected


def test_retry_without_waiting_does_not_promise_a_pause():
    """Смена выдохшегося ключа идет без паузы - и обещать ее в чате незачем."""
    stage = stages.Stage(stages.RETRY, 2, 15, None, errors.ERROR_KIND_DAILY)

    assert status.render_stage(stage) == (
        "⏳ Ключ выдохся, беру следующий. Попытка 2 из 15..."
    )


def test_report_without_progress_does_nothing():
    """
    progress=None - штатный случай, а не недосмотр: слой gemini зовут и без Telegram.

    Ни исключения, ни попытки что-то отправить: рассказывать просто некому.
    """
    asyncio.run(stages.report(None, stages.Stage(stages.ASKING)))


def test_broken_progress_does_not_reach_the_caller(caplog):
    """
    Сломанный получатель этапов остается своей бедой и генерацию ответа не рвет.

    Последняя сетка на случай, если получатель окажется не таким аккуратным, как
    karachur.tg.status: человеку нужен ответ, а не красивый статус.
    """

    async def _boom(_stage):
        """Получатель, который падает на любом этапе."""
        raise RuntimeError("получатель этапов сломан")

    with caplog.at_level(logging.WARNING, logger="karachur.gemini.stages"):
        asyncio.run(stages.report(_boom, stages.Stage(stages.ASKING)))

    assert "получатель этапов сломан" in caplog.text


def test_last_stage_is_shown_even_without_a_next_one():
    """
    Этап, пришедший раньше срока, показывается сам, не дожидаясь следующего.

    Случай не выдуманный и самый неприятный из возможных: "собираю контекст", "готовлю
    вложения" и "спрашиваю модель" укладываются в пару секунд, два последних попадают в
    промежуток, а дальше модель думает минуты. Без отложенной правки человек все это
    время видел бы "собираю контекст" и решил бы, что бот завис.
    """
    placeholder = _FakePlaceholder()
    live = status.StatusMessage(placeholder, min_interval=0.05)

    async def _run():
        """Два этапа подряд, между ними ничего не ждем."""
        await live(stages.Stage(stages.CONTEXT))
        await live(stages.Stage(stages.ASKING))
        # Своих вызовов больше не будет - показать второй этап может только отложенная.
        await asyncio.sleep(0.2)

    asyncio.run(_run())

    assert placeholder.texts == [
        "⏳ Собираю контекст чата...",
        "⏳ Спрашиваю модель...",
    ]


def test_close_stops_the_deferred_edit():
    """
    Погашенная отложенная правка не переписывает готовый ответ.

    Заглушка - то же самое сообщение, в которое потом ложится ответ. Проснувшаяся после
    отправки задача затерла бы ответ служебной строкой, и человек остался бы с
    "спрашиваю модель" вместо того, что спросил.
    """
    placeholder = _FakePlaceholder()
    live = status.StatusMessage(placeholder, min_interval=0.05)

    async def _run():
        """Второй этап уходит в отложенную правку, но ее гасят до срока."""
        await live(stages.Stage(stages.CONTEXT))
        await live(stages.Stage(stages.ASKING))
        live.close()
        await asyncio.sleep(0.2)

    asyncio.run(_run())

    assert placeholder.texts == ["⏳ Собираю контекст чата..."]
