"""
Повторы запросов к Gemini: что делать, когда ответа с первого раза не вышло.

Ошибка ошибке рознь, и весь смысл модуля - развести реакции на них. Читает ошибку
karachur.gemini.errors, состояние ключей ведет karachur.gemini.pool, а здесь решается,
что из этого следует для самого запроса: переждать и повторить тем же ключом, уйти на
следующий ключ или сдаться сразу, не перебирая пул зря.

Пустой ответ - такая же неудача, как исключение: модель иногда отвечает без текста, и
снаружи это ничем не лучше ошибки API. Поэтому разбор ответа живет тут же, рядом с
повторами, а не у того, кто запрос заказывал.
"""

import asyncio
import logging
import random
import re

from karachur import config
from karachur.gemini import errors

# Модуль зовется key_pool, а не pool: имя pool по всему коду занято самим пулом чата
# (аргументы обработчиков, поле сессии), и модуль под тем же именем ими бы перекрывался.
from karachur.gemini import pool as key_pool

logger = logging.getLogger(__name__)

# В деталях ошибки 429 Gemini присылает рекомендованную паузу: "retryDelay": "27s".
RETRY_DELAY_PATTERN = re.compile(
    r"retry[-_]?delay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)s", re.IGNORECASE
)


class GeminiRetryError(Exception):
    """Не удалось получить корректный ответ от Gemini за отведенное число попыток."""


def get_backoff_delay(
    attempt: int, base_delay: float, max_delay: float, exc: Exception | None = None
) -> float:
    """
    Считает паузу перед следующей попыткой.

    :param attempt: номер только что провалившейся попытки (начиная с единицы)
    :type attempt: int
    :param base_delay: пауза после первой неудачи, дальше растет вдвое за попытку
    :type base_delay: float
    :param max_delay: потолок, выше которого пауза не поднимается
    :type max_delay: float
    :param exc: исключение, если попытка упала с ошибкой API
    :type exc: Exception | None
    :return: длительность паузы в секундах
    :rtype: float
    """
    delay = base_delay * (2 ** (attempt - 1))
    if exc is not None:
        # Если API сам сказал, сколько ждать (429), слушаемся его.
        match = RETRY_DELAY_PATTERN.search(str(exc))
        if match:
            delay = float(match.group(1))
    delay = min(delay, max_delay)
    # Джиттер, чтобы повторы не выстраивались в ровную сетку.
    return delay + random.uniform(0, delay * 0.1)


def extract_response_text(response) -> tuple[str | None, str, bool]:
    """
    Достает текст из ответа Gemini и объясняет, если текста нет.

    :param response: ответ метода generate_content
    :return: (текст или None, описание проблемы, стоит ли повторять запрос)
    :rtype: tuple[str | None, str, bool]
    """
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(feedback, "block_reason", None) if feedback else None
    if block_reason:
        # Блокировка самого запроса детерминирована - повтор ничего не изменит.
        return None, f"запрос заблокирован фильтрами ({block_reason})", False

    try:
        text = response.text
    except Exception as e:  # pylint: disable=broad-exception-caught
        return None, f"не удалось прочитать текст ответа: {e}", True

    if text and text.strip():
        return text, "", True

    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    return None, f"модель вернула пустой текст (finish_reason={finish})", True


def handle_api_failure(
    pool: key_pool.KeyPool, key: dict, exc: Exception, attempt: int, max_retries: int
) -> Exception | None:
    """
    Разбирает ошибку API: помечает ключ и решает, стоит ли ждать перед повтором.

    :param pool: пул ключей чата
    :type pool: key_pool.KeyPool
    :param key: ключ, на котором упал запрос
    :type key: dict
    :param exc: пойманное исключение
    :type exc: Exception
    :param attempt: номер попытки - уходит в текст неустранимой ошибки
    :type attempt: int
    :param max_retries: всего попыток - тоже только ради текста ошибки
    :type max_retries: int
    :return: исключение, если ошибка временная и перед повтором надо выждать паузу,
        или None, если ждать нечего: ключ уже помечен негодным и сменится сам
    :rtype: Exception | None
    :raises GeminiRetryError: если повторять бессмысленно
    """
    kind = errors.classify_api_error(exc)
    code = errors.get_error_code(exc)

    if kind == errors.ERROR_KIND_FATAL:
        logger.error("Неустранимая ошибка Gemini (код %s): %s", code, exc)
        raise GeminiRetryError(
            f"Неустранимая ошибка API на попытке {attempt} из {max_retries} "
            f"(код {code}): {exc}"
        ) from exc

    if kind == errors.ERROR_KIND_DAILY:
        pool.mark_daily_exhausted(key)
        return None
    if kind == errors.ERROR_KIND_KEY:
        pool.mark_broken(key, exc)
        return None

    # Минутный лимит и временные сбои: ключ живой, надо просто подождать.
    return exc


async def generate_with_retries(
    cfg: config.Config, pool: key_pool.KeyPool, make_contents
) -> str:
    """
    Запрашивает ответ у Gemini, повторяя попытки при сбоях и меняя выдохшиеся ключи.

    Повторяет до cfg.max_retries раз с экспоненциально растущей паузой. Ошибка ошибке
    рознь: выбранная дневная квота и отвергнутый ключ означают, что надо брать следующий
    ключ и идти дальше без паузы; минутный лимит - что ключ живой и надо просто подождать;
    кривой запрос или несуществующая модель не пройдут никогда, и на них бот сдается.

    Смена ключа тратит попытку. Так цикл не может закружиться на пуле из сотни мертвых
    ключей, а на живом пуле лишние попытки и не понадобятся.

    :param cfg: настройки бота - отсюда берутся число попыток и длина пауз
    :type cfg: config.Config
    :param pool: пул ключей чата, он же задает модель запроса
    :type pool: key_pool.KeyPool
    :param make_contents: корутина, собирающая содержимое запроса под переданный ключ
    :return: текст ответа модели
    :rtype: str
    :raises GeminiRetryError: если попытки исчерпаны
    :raises key_pool.NoUsableKeys: если в чате не осталось рабочих ключей
    """
    last_reason = "причина неизвестна"
    contents = None
    contents_key_id = None

    for attempt in range(1, cfg.max_retries + 1):
        key = pool.active()
        if contents is None or contents_key_id != key["id"]:
            # Ссылки на выгруженные файлы принадлежат тому ключу, которым их выгружали,
            # поэтому после смены ключа запрос собирается заново.
            contents = await make_contents(key)
            contents_key_id = key["id"]

        failure = None
        try:
            # Вызов синхронный, уводим его в поток, чтобы не морозить event loop.
            response = await asyncio.to_thread(
                pool.client(key).models.generate_content,
                model=pool.model,
                contents=contents,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            last_reason = f"ошибка API {errors.get_error_code(e)}: {e}"
            failure = handle_api_failure(pool, key, e, attempt, cfg.max_retries)
        else:
            pool.note_request(key)
            text, reason, can_retry = extract_response_text(response)
            if text:
                if attempt > 1:
                    logger.info(
                        "Ответ получен с попытки %d из %d.", attempt, cfg.max_retries
                    )
                return text
            if not can_retry:
                logger.error("Повтор бесполезен: %s", reason)
                raise GeminiRetryError(
                    f"Повтор бесполезен, остановились на попытке {attempt} "
                    f"из {cfg.max_retries}: {reason}"
                )
            last_reason = reason

        logger.warning(
            "Попытка %d из %d не удалась: %s", attempt, cfg.max_retries, last_reason
        )

        # Ключ уже помечен негодным - ждать нечего, следующий виток возьмет другой.
        if not pool.is_usable(key, ignore_local_limit=True):
            continue

        if attempt < cfg.max_retries:
            delay = get_backoff_delay(
                attempt, cfg.retry_base_delay, cfg.retry_max_delay, failure
            )
            logger.info("Повтор через %.1f с.", delay)
            await asyncio.sleep(delay)

    raise GeminiRetryError(
        f"Не удалось получить ответ за {cfg.max_retries} попыток. "
        f"Последняя причина: {last_reason}"
    )
