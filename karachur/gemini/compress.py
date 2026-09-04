"""
Сжатие контекста: как уместить бесконечную историю чата в конечный лимит модели.

История чата не кончается, а контекст модели конечен. Когда запрос перестает влезать,
самая старая его часть уходит той же модели на пересказ, пересказ ложится в БД и
занимает ее место в следующих запросах. Так чат помнит, о чем говорили месяц назад, не
таща с собой каждую реплику дословно.

Сжатие - оптимизация, а не обязательный шаг: ни одна неудача здесь не должна мешать
боту ответить. Не посчитались токены, не вышел пересказ - контекст уходит как есть, и
дальше с ним разбирается обычный механизм повторов.

Сжатие - самая незаметная часть долгого ожидания: снаружи оно ничем не отличается от
обычного запроса, а стоит целого разговора с моделью, да еще и с повторами. Поэтому и
проход сжатия, и подсчет токенов, и выгрузка вложений сообщаются наружу колбэком progress
(karachur.gemini.stages) - человек в чате видит, что бот занят делом.

Размер контекста меряется двумя разными способами, и это не дублирование. Дешевая
прикидка по длине текста стоит на входе: обычный чат до лимита не дотягивает, и платить
за это лишним запросом к API незачем. Внутри цикла считает уже токенайзер - ошибись там
прикидка, и сжатие остановилось бы, не дойдя до лимита, или наоборот жевало бы историю
впустую.
"""

import asyncio
import logging
import sqlite3

from google import genai

from karachur import config

# Модуль зовется request_contents, а не contents: имя contents тут занято самими
# собираемыми списками содержимого запроса, и модуль ими бы перекрывался.
from karachur.gemini import contents as request_contents

# Ровно та же история с пулом: имя pool по всему коду занято самим пулом чата
# (аргументы обработчиков, поле сессии), и модуль под тем же именем ими бы перекрывался.
from karachur.gemini import pool as key_pool
from karachur.gemini import retries, stages
from karachur.storage import summaries

logger = logging.getLogger(__name__)

# Задача на сжатие. Уходит первой репликой, чтобы история читалась уже с ней в голове.
SUMMARY_INSTRUCTION = (
    "[Служебная задача] Дальше идет начало истории группового чата, которое надо сжать, "
    "чтобы освободить место в контексте. Перескажи ее связным текстом на русском языке. "
    "Сохрани: кто участвовал и чем запомнился, о чем договорились, факты об участниках и "
    "чате, клички, шутки и отсылки, которые всплывают в разговоре, содержание присланных "
    "файлов и картинок, незакрытые вопросы и обещания. Опусти пустую болтовню и "
    'приветствия. Пиши по существу, без вступлений вроде "вот пересказ". Не отвечай на '
    "реплики из истории и не обращайся к участникам: это служебная выжимка для тебя же, "
    "а не сообщение в чат."
)
# Финальная реплика запроса на сжатие: историю уже показали, осталось попросить результат.
SUMMARY_REQUEST = "Сделай пересказ показанной истории по служебной задаче выше."


def estimate_entry_tokens(cfg: config.Config, entry: dict) -> float:
    """
    Грубо оценивает размер одной реплики в токенах.

    Прикидка и решение о границе сжатия завязаны сразу на несколько настроек, поэтому
    сюда и соседям по модулю уезжает весь cfg: перечислять их поштучно в сигнатурах
    вышло бы длиннее, чем сами функции.

    :param cfg: настройки бота - отсюда берутся вес символа и вес медиа
    :type cfg: config.Config
    :param entry: реплика из build_history
    :type entry: dict
    :return: примерное число токенов
    :rtype: float
    """
    total = 0.0
    for part in entry["parts"]:
        text = getattr(part, "text", None)
        total += len(text) / cfg.chars_per_token if text else cfg.media_token_estimate
    return total


def estimate_context_tokens(
    cfg: config.Config, history: list, summary: str | None
) -> float:
    """
    Грубо оценивает размер всего контекста, чтобы не дергать API на каждое сообщение.

    :param cfg: настройки бота - отсюда берутся системный промпт и веса оценки
    :type cfg: config.Config
    :param history: история переписки от build_history
    :type history: list
    :param summary: пересказ сжатой части истории или None
    :type summary: str | None
    :return: примерное число токенов
    :rtype: float
    """
    total = len(cfg.system_prompt) / cfg.chars_per_token
    if summary:
        total += len(summary) / cfg.chars_per_token
    return total + sum(estimate_entry_tokens(cfg, entry) for entry in history)


async def count_context_tokens(
    pool: key_pool.KeyPool, key: dict, contents: list
) -> int | None:
    """
    Считает точный размер запроса токенайзером Gemini.

    :param pool: пул ключей чата
    :type pool: key_pool.KeyPool
    :param key: ключ, которым идем в API
    :type key: dict
    :param contents: подготовленное содержимое запроса
    :type contents: list
    :return: число токенов или None, если посчитать не вышло
    :rtype: int | None
    """
    try:
        # Вызов синхронный, уводим его в поток, чтобы не морозить event loop.
        response = await asyncio.to_thread(
            pool.client(key).models.count_tokens, model=pool.model, contents=contents
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Не удалось посчитать токены контекста: %s", e)
        return None
    return getattr(response, "total_tokens", None)


def choose_cut_index(cfg: config.Config, history: list, total_tokens: int) -> int:
    """
    Решает, сколько самых старых реплик отправить в пересказ.

    Отрезаем не половину наугад, а столько, чтобы остаток уложился в целевую долю
    лимита. Вес реплик берем оценочный: точные размеры кусков нам взять неоткуда,
    а промах компенсирует повторный проход сжатия.

    :param cfg: настройки бота - отсюда лимит контекста, целевая доля и неприкосновенный
        хвост свежих сообщений
    :type cfg: config.Config
    :param history: история переписки от build_history
    :type history: list
    :param total_tokens: точный размер контекста, который не влез в лимит
    :type total_tokens: int
    :return: сколько реплик с начала истории надо сжать (0 - сжимать нечего)
    :rtype: int
    """
    weights = [estimate_entry_tokens(cfg, entry) for entry in history]
    keep_weight = sum(weights) * (
        cfg.max_context_tokens * cfg.context_target_ratio / total_tokens
    )

    # Идем с конца и набираем хвост, который оставляем дословно.
    tail_weight = 0.0
    cut = len(history)
    for index in range(len(history) - 1, -1, -1):
        if tail_weight + weights[index] > keep_weight:
            break
        tail_weight += weights[index]
        cut = index

    # Свежие реплики не сжимаем никогда, даже если одна из них весит больше лимита.
    return min(cut, max(len(history) - cfg.keep_recent_messages, 0))


async def summarize_history(
    cfg: config.Config,
    pool: key_pool.KeyPool,
    context_messages: list,
    previous_summary: str | None,
    progress: stages.Progress | None = None,
) -> str:
    """
    Просит модель пересказать кусок истории одним текстом.

    Предыдущий пересказ идет в запрос вместе с историей, чтобы он не потерялся:
    новый пересказ заменяет его целиком.

    :param cfg: настройки бота
    :type cfg: config.Config
    :param pool: пул ключей чата
    :type pool: key_pool.KeyPool
    :param context_messages: сообщения, которые надо сжать
    :type context_messages: list
    :param previous_summary: прошлый пересказ или None, если сжимаем впервые
    :type previous_summary: str | None
    :param progress: кому рассказывать об этапах; None - рассказывать некому
    :type progress: stages.Progress | None
    :return: текст нового пересказа
    :rtype: str
    :raises retries.GeminiRetryError: если модель так и не ответила
    """

    async def make_contents(key: dict) -> list:
        """Собирает запрос на пересказ под конкретный ключ."""
        # Про выгрузку вложений говорим до, а не внутри: build_history крутится в
        # отдельном потоке, и await оттуда недоступен.
        await stages.report(progress, stages.Stage(stages.ATTACHMENTS))
        entries = await asyncio.to_thread(
            request_contents.build_history, key, context_messages, cfg.media_dir
        )
        contents = [
            genai.types.ContentDict(
                role="user", parts=[genai.types.PartDict(text=SUMMARY_INSTRUCTION)]
            )
        ]
        if previous_summary:
            contents.append(
                genai.types.ContentDict(
                    role="user",
                    parts=[
                        genai.types.PartDict(
                            text=f"{request_contents.SUMMARY_HEADER}\n"
                            f"{previous_summary}"
                        )
                    ],
                )
            )
        for entry in entries:
            contents.append(
                genai.types.ContentDict(role=entry["role"], parts=entry["parts"])
            )
        contents.append(
            genai.types.ContentDict(
                role="user", parts=[genai.types.PartDict(text=SUMMARY_REQUEST)]
            )
        )
        return contents

    logger.info(
        "Сжимаем %d самых старых сообщений в пересказ...", len(context_messages)
    )
    return (
        await retries.generate_with_retries(cfg, pool, make_contents, progress)
    ).strip()


async def compress_context(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    cfg: config.Config,
    pool: key_pool.KeyPool,
    conn: sqlite3.Connection,
    chat_id: int,
    context_messages: list,
    summary: str | None,
    progress: stages.Progress | None = None,
) -> tuple[list, str | None]:
    """
    Ужимает контекст чата до лимита и возвращает то, что в него уложилось.

    Пока запрос не влезает в cfg.max_context_tokens, самая старая часть истории уходит
    модели на пересказ, пересказ попадает в БД и занимает ее место. Один проход не
    всегда доводит до цели (пересказ тоже занимает место), поэтому проходов может
    быть несколько.

    Сжатие - это оптимизация, а не обязательный шаг: если посчитать токены или
    получить пересказ не вышло, отдаем контекст как есть и даем разбираться
    обычному механизму повторов.

    :param cfg: настройки бота - отсюда лимит контекста и правила сжатия
    :type cfg: config.Config
    :param pool: пул ключей чата, он же задает модель запроса
    :type pool: key_pool.KeyPool
    :param conn: соединение с базой данных
    :type conn: sqlite3.Connection
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param context_messages: сообщения контекста (непустой список)
    :type context_messages: list
    :param summary: пересказ сжатой ранее части истории или None
    :type summary: str | None
    :param progress: кому рассказывать об этапах; None - рассказывать некому
    :type progress: stages.Progress | None
    :return: (оставшиеся дословно сообщения, актуальный пересказ)
    :rtype: tuple[list, str | None]
    """
    key = pool.active()
    # Про выгрузку вложений говорим до сборки, а не внутри нее: build_history уходит в
    # отдельный поток, и await оттуда недоступен.
    await stages.report(progress, stages.Stage(stages.ATTACHMENTS))
    history = await asyncio.to_thread(
        request_contents.build_history, key, context_messages, cfg.media_dir
    )

    # Дешевая прикидка на входе: обычный чат до лимита не дотягивает, и тратить на него
    # лишний запрос к API незачем. Дальше по кругу идем уже только с точным подсчетом -
    # ошибись прикидка, и сжатие остановилось бы, не дойдя до лимита.
    if estimate_context_tokens(cfg, history, summary) < (
        cfg.max_context_tokens * cfg.token_check_ratio
    ):
        return context_messages, summary

    for round_number in range(1, cfg.max_compression_rounds + 1):
        # Пересказ мог упереться в квоту и сменить ключ: ссылки на выгруженные файлы
        # принадлежат прежнему ключу, поэтому историю приходится пересобрать.
        current_key = pool.active()
        if current_key["id"] != key["id"]:
            key = current_key
            await stages.report(progress, stages.Stage(stages.ATTACHMENTS))
            history = await asyncio.to_thread(
                request_contents.build_history, key, context_messages, cfg.media_dir
            )

        await stages.report(progress, stages.Stage(stages.MEASURING))
        total_tokens = await count_context_tokens(
            pool,
            key,
            request_contents.build_contents(history, summary, cfg.system_prompt),
        )
        if total_tokens is None:
            return context_messages, summary
        if total_tokens <= cfg.max_context_tokens:
            logger.info(
                "Контекст: %d токенов из %d.", total_tokens, cfg.max_context_tokens
            )
            return context_messages, summary

        cut = choose_cut_index(cfg, history, total_tokens)
        if cut <= 0:
            logger.warning(
                "Контекст (%d токенов) больше лимита %d, но сжимать уже нечего.",
                total_tokens,
                cfg.max_context_tokens,
            )
            return context_messages, summary

        logger.info(
            "Контекст разросся до %d токенов при лимите %d, сжимаем.",
            total_tokens,
            cfg.max_context_tokens,
        )
        compressed = [entry["source"] for entry in history[:cut]]
        await stages.report(
            progress,
            stages.Stage(stages.COMPRESS, round_number, cfg.max_compression_rounds),
        )
        try:
            summary = await summarize_history(cfg, pool, compressed, summary, progress)
        except retries.GeminiRetryError as e:
            logger.error("Не удалось сжать контекст, отправляем как есть: %s", e)
            return context_messages, summary

        summaries.save_summary(
            conn, chat_id, summary, [msg["message_id"] for msg in compressed]
        )
        history = history[cut:]
        context_messages = [entry["source"] for entry in history]

    logger.warning(
        "Контекст не уложился в лимит за %d проходов.", cfg.max_compression_rounds
    )
    return context_messages, summary
