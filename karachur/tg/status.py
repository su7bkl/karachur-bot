"""
Живая заглушка: что бот пишет о себе, пока готовит ответ.

Заглушку отправляет karachur.tg.delivery, а этот модуль превращает ее из надписи в
рассказ. Слой gemini по ходу работы сообщает об этапах структурами
karachur.gemini.stages.Stage, здесь они становятся строкой и уезжают в правку заглушки -
человек видит, идет ли дело и на чем оно стоит, а не одну и ту же строку все восемнадцать
минут. Готовый ответ потом занимает место заглушки как раньше, через replace_placeholder.

Текст пишется тут, а не в слое gemini, и это главная граница модуля: там знают, что
случилось (вид ошибки, номер попытки), а как это назвать человеку - решается здесь.
Поэтому же причина повтора приезжает константой errors.ERROR_KIND_*, а в чат уходит
"модель занята" или "ключ выдохся" - код ошибки участникам чата ничего не говорит.

Две беды подстерегают всякого, кто правит одно сообщение часто, и обе учтены.

Первая - лимиты Telegram. Слишком частые правки он отбивает, а на упорство отвечает
временной блокировкой бота в чате, и тогда до чата не доедет уже и сам ответ. Поэтому
между правками выдерживается EDIT_INTERVAL, а этапы, пришедшие раньше срока,
не копятся в очередь: заглушке нужен текущий этап, а не пересказ пропущенных, - поэтому
хранится только последний, и ближайшая разрешенная правка покажет именно его.

Вторая - "message is not modified": на правку, которая ничего не меняет, Telegram
отвечает ошибкой. Отсюда shown - то, что в заглушке уже написано, и правка с тем же
текстом просто не отправляется.

Часы вынесены отдельной функцией now намеренно: тест подменяет ее и проверяет промежуток
между правками, не выжидая его по-настоящему.
"""

import asyncio
import logging
import time

from telegram import Message
from telegram.error import TelegramError

from karachur.gemini import errors, stages
from karachur.tg import delivery

logger = logging.getLogger(__name__)

# Минимальный промежуток между правками заглушки в секундах. Три - с запасом: Telegram
# считает правки наравне с сообщениями и терпит примерно одно в секунду на чат, а этапы
# в пиковые моменты (сборка запроса, начало попытки) идут очередями по несколько штук
# подряд.
EDIT_INTERVAL = 3.0

# Этапы, которым хватает одной строки без подробностей.
STAGE_TEXTS = {
    stages.CONTEXT: "⏳ Собираю контекст чата...",
    stages.ATTACHMENTS: "⏳ Готовлю вложения...",
    stages.MEASURING: "⏳ Считаю размер контекста...",
    stages.ASKING: "⏳ Спрашиваю модель...",
    stages.TRANSCRIBE: "⏳ Расшифровываю голосовое...",
}

# Почему пошли на повтор - человеческими словами. Слева константы karachur.gemini.errors:
# там эти виды ошибок уже разведены, и заводить свою классификацию тут незачем.
RETRY_REASONS = {
    errors.ERROR_KIND_RATE: "Модель занята",
    errors.ERROR_KIND_DAILY: "Ключ выдохся, беру следующий",
    errors.ERROR_KIND_KEY: "Ключ отвергнут, беру следующий",
    errors.ERROR_KIND_FILE: "Вложение потерялось, прикладываю заново",
    errors.ERROR_KIND_TRANSIENT: "Временный сбой",
}
# Повтор без ошибки API: модель ответила пустотой, и объяснять тут нечего.
UNKNOWN_RETRY_REASON = "Модель не ответила"


def now() -> float:
    """
    Возвращает текущее время по монотонным часам.

    Обернуто функцией ради тестов: подменить ее дешевле, чем ждать промежуток между
    правками по-настоящему, а подменять сам time.monotonic нельзя - по нему живет
    планировщик asyncio.

    :return: моментальный отсчет в секундах
    :rtype: float
    """
    return time.monotonic()


def render_stage(stage: stages.Stage) -> str:
    """
    Превращает этап в строку для заглушки.

    :param stage: этап от слоя gemini
    :type stage: stages.Stage
    :return: текст, который увидит человек в чате
    :rtype: str
    """
    if stage.name == stages.COMPRESS:
        return f"⏳ Сжимаю историю чата (проход {stage.number} из {stage.total})..."

    if stage.name == stages.RETRY:
        reason = RETRY_REASONS.get(stage.error_kind, UNKNOWN_RETRY_REASON)
        text = f"⏳ {reason}. Попытка {stage.number} из {stage.total}"
        if stage.delay:
            # Доли секунды человеку не нужны, а "через 0 с" выглядело бы враньем.
            text += f" через {max(1, round(stage.delay))} с"
        return f"{text}..."

    # Неизвестный этап - не повод писать в чат ерунду: остается обычная заглушка.
    return STAGE_TEXTS.get(stage.name, delivery.GENERATING_PLACEHOLDER)


# Забота у объекта ровно одна - держать заглушку в актуальном виде, и наружу он смотрит
# одним вызовом: его же и передают в слой gemini как progress. Дробить тут нечего.
class StatusMessage:
    """
    Заглушка, которая рассказывает о ходе дела.

    Объект живет ровно один ответ: его заводят сразу после отправки заглушки и передают
    в слой gemini как progress. Заглушки может и не быть вовсе (send_placeholder
    вернул None, если отправить не удалось) - тогда объект молча ничего не делает, и
    звать его все равно можно.
    """

    def __init__(
        self, placeholder: Message | None, min_interval: float = EDIT_INTERVAL
    ):
        """
        :param placeholder: сообщение-заглушка или None, если ее не удалось отправить
        :type placeholder: Message | None
        :param min_interval: минимальный промежуток между правками в секундах
        :type min_interval: float
        """
        self.placeholder = placeholder
        self.min_interval = min_interval
        # Что в заглушке написано сейчас: с этим сверяется каждая правка, чтобы не
        # отправить тот же текст второй раз.
        self.shown = delivery.GENERATING_PLACEHOLDER
        # Последний известный этап. Хранится именно он один: если правка отложена по
        # промежутку, ближайшая разрешенная покажет текущее положение дел, а не очередь
        # устаревших этапов.
        self.pending = self.shown
        # Когда правили в последний раз; None - еще ни разу, и первая правка идет сразу:
        # заглушка только что отправлена, и лишний виток ожидания человеку не поможет.
        self.edited_at: float | None = None
        # Отложенная правка. Заводится, когда этап пришел раньше срока: без нее он так и
        # остался бы непоказанным, если следующего этапа не случится. А не случиться его
        # может надолго - ровно перед самым долгим ожиданием, ответом модели.
        self.deferred: asyncio.Task | None = None

    async def __call__(self, stage: stages.Stage):
        """
        Принимает этап от слоя gemini и, если можно, показывает его в заглушке.

        :param stage: этап работы
        :type stage: stages.Stage
        """
        self.pending = render_stage(stage)
        if self.placeholder is None:
            return
        if self.edited_at is not None and now() - self.edited_at < self.min_interval:
            # Правим слишком часто. Этап уже запомнен, осталось показать его, когда
            # промежуток выйдет: следующего вызова может и не быть, а молча показывать
            # человеку устаревший этап все то время, пока модель думает, - хуже всего.
            self._defer(self.min_interval - (now() - self.edited_at))
            return
        await self._edit()

    def _defer(self, delay: float):
        """
        Заводит отложенную правку, если она еще не заведена.

        :param delay: сколько ждать до правки в секундах
        :type delay: float
        """
        if self.deferred is not None and not self.deferred.done():
            # Одной хватит: проснувшись, она покажет самый свежий этап на тот момент.
            return

        async def show_later():
            await asyncio.sleep(delay)
            await self._edit()

        self.deferred = asyncio.create_task(show_later())

    def close(self):
        """
        Гасит отложенную правку.

        Зовется перед тем, как заглушку заменят готовым ответом. Без этого проснувшаяся
        задача переписала бы уже отданный человеку ответ обратно в служебную строку.
        """
        if self.deferred is not None and not self.deferred.done():
            self.deferred.cancel()

    async def _edit(self):
        """Отправляет накопленный текст в заглушку, если он отличается от показанного."""
        if self.pending == self.shown:
            # Telegram отвечает ошибкой на правку, которая ничего не меняет.
            return

        text = self.pending
        # Время правки отмечаем до самой правки: неудачная попытка стоит Telegram
        # ровно того же запроса, и повторять ее без паузы - верный способ добить лимит.
        self.edited_at = now()
        try:
            await self.placeholder.edit_text(text)
        except TelegramError as e:
            # Заглушку могли удалить, чат закрыть, правку отбить по лимиту. Ни одна из
            # этих бед не стоит ответа, поэтому дальше лога она не идет.
            logger.warning("Не удалось обновить статус: %s", e)
            return
        self.shown = text
