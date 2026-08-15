"""
Команды бота: пул ключей Gemini и выбор модели.

Команды доступны всем участникам чата - так решено при постановке задачи. Единственная
предосторожность касается /addkey: сообщение с ключом бот сразу удаляет, чтобы ключ не
остался висеть в истории чата у всех на виду. Само сообщение с командой в базу не
попадает - обработчик обычных сообщений команды не видит.

Ключи в ответах всегда замаскированы: в чат уходят только концы ключа, по которым его
можно опознать в /keys и назвать в /delkey.
"""

import asyncio
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import api_keys
import chat_settings

logger = logging.getLogger(__name__)

HELP_TEXT = """Команды бота:

/keys - показать ключи Gemini этого чата и их дневные счетчики
/addkey КЛЮЧ - добавить ключ в пул чата (сообщение с ключом будет удалено)
/delkey НОМЕР|ХВОСТ - убрать ключ из пула чата
/rotatekey - принудительно переключиться на следующий ключ
/model - показать текущую модель и список доступных
/model ИМЯ - переключить чат на другую модель
/model default - вернуться к модели из настроек бота
/help - эта справка

Ключи относятся к чату: у каждого чата свой пул, своя история и своя модель. Когда ключ
выбирает дневную квоту, бот сам переходит к следующему, а после полуночи по
тихоокеанскому времени счетчики обнуляются."""

# Модель, которую /model понимает как "вернуться к настройке из config.cfg".
DEFAULT_MODEL_ALIASES = frozenset({"default", "по умолчанию", "сброс", "reset"})


def _pool(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> api_keys.KeyPool:
    """
    Собирает пул ключей чата из общих данных бота.

    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    :param chat_id: идентификатор чата
    :type chat_id: int
    :return: пул ключей
    :rtype: api_keys.KeyPool
    """
    return api_keys.KeyPool(
        context.bot_data["db_conn"], chat_id, context.bot_data["key_rpd_limit"]
    )


def list_models(api_key: str) -> list[str]:
    """
    Спрашивает у Gemini, какие модели доступны этому ключу.

    :param api_key: ключ Gemini
    :type api_key: str
    :return: имена моделей, умеющих отвечать на запросы
    :rtype: list[str]
    """
    client = api_keys.client_for_key(api_key)
    names = []
    for model in client.models.list():
        actions = getattr(model, "supported_actions", None)
        # Пустой список действий встречается у части моделей - отбрасывать их по нему
        # нельзя, иначе выборка окажется пустой на ровном месте.
        if actions and "generateContent" not in actions:
            continue
        name = (model.name or "").removeprefix("models/")
        if name:
            names.append(name)
    return sorted(names)


async def _delete_message(update: Update):
    """
    Убирает из чата сообщение с командой.

    :param update: объект обновления Telegram
    :type update: Update
    """
    try:
        await update.effective_message.delete()
    except TelegramError as e:
        logger.warning("Не удалось удалить сообщение с ключом: %s", e)


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """
    Отправляет ответ на команду обычным сообщением в чат.

    Отвечаем не реплаем, а отдельным сообщением: сообщение с /addkey к этому моменту
    уже удалено, и отвечать в него некуда.

    :param update: объект обновления Telegram
    :type update: Update
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    :param text: текст ответа
    :type text: str
    """
    await context.bot.send_message(update.effective_chat.id, text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает справку по командам.

    :param update: объект обновления Telegram
    :type update: Update
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    """
    await _reply(update, context, HELP_TEXT)


async def keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает ключи чата, их состояние и дневные счетчики.

    :param update: объект обновления Telegram
    :type update: Update
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    """
    chat_id = update.effective_chat.id
    pool = _pool(context, chat_id)
    keys = pool.keys()
    if not keys:
        await _reply(
            update,
            context,
            "У чата нет ни одного ключа Gemini. Добавьте его командой /addkey КЛЮЧ",
        )
        return

    try:
        # Спрашиваем не записанный указатель, а тот ключ, который взял бы следующий
        # запрос: пока чат ни разу не ходил в API, указателя еще нет, и без этого
        # /keys не пометил бы активным никого.
        active_id = pool.active()["id"]
    except api_keys.NoUsableKeys:
        # Все ключи выбыли - помечать активным нечего.
        active_id = None

    lines = [
        api_keys.describe_key(key, index, active_id, pool.daily_limit)
        for index, key in enumerate(keys, start=1)
    ]
    model = chat_settings.get_model(
        context.bot_data["db_conn"], chat_id, context.bot_data["default_model"]
    )
    lines.append("")
    lines.append(f"Модель чата: {model}")
    await _reply(update, context, "\n".join(lines))


async def add_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Добавляет ключ Gemini в пул чата.

    Сообщение с командой удаляется сразу, до всех проверок: даже если ключ окажется
    негодным, висеть в истории чата он не должен.

    :param update: объект обновления Telegram
    :type update: Update
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    """
    args = context.args
    await _delete_message(update)

    if not args:
        await _reply(update, context, "Формат: /addkey ВАШ_КЛЮЧ_GEMINI")
        return

    api_key = args[0].strip()
    chat_id = update.effective_chat.id
    conn = context.bot_data["db_conn"]

    problem = await _probe_key(api_key)
    if problem:
        await _reply(update, context, problem)
        return

    key, already = api_keys.add_key(conn, chat_id, api_key)
    if already:
        await _reply(
            update, context, f"Ключ {api_keys.mask_key(api_key)} уже есть в пуле чата."
        )
        return

    total = len(api_keys.list_chat_keys(conn, chat_id))
    await _reply(
        update,
        context,
        f"Ключ {api_keys.mask_key(key['api_key'])} добавлен. Ключей в пуле: {total}.",
    )


async def _probe_key(api_key: str) -> str | None:
    """
    Проверяет ключ живым запросом к API до того, как он попадет в пул.

    Негодный ключ иначе всплыл бы только в момент ответа пользователю - и то не сразу,
    а после того, как бот честно переберет из-за него все попытки.

    :param api_key: ключ Gemini
    :type api_key: str
    :return: текст отказа или None, если ключ можно добавлять
    :rtype: str | None
    """
    try:
        await asyncio.to_thread(list_models, api_key)
    except Exception as e:  # pylint: disable=broad-exception-caught
        if api_keys.classify_api_error(e) == api_keys.ERROR_KIND_KEY:
            return f"Gemini не принял этот ключ: {e}"
        # Сеть легла или API прилегло - ключ в этом не виноват, берем как есть.
        logger.warning("Не удалось проверить ключ, добавляем без проверки: %s", e)
    return None


async def delete_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Убирает ключ из пула чата.

    :param update: объект обновления Telegram
    :type update: Update
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    """
    chat_id = update.effective_chat.id
    conn = context.bot_data["db_conn"]
    keys = api_keys.list_chat_keys(conn, chat_id)

    if not context.args:
        await _reply(
            update,
            context,
            "Формат: /delkey НОМЕР или /delkey ХВОСТ_КЛЮЧА. Номера покажет /keys",
        )
        return
    if not keys:
        await _reply(update, context, "У чата нет ключей - удалять нечего.")
        return

    key = api_keys.find_key(keys, context.args[0])
    if key is None:
        await _reply(
            update,
            context,
            "Такого ключа в пуле чата нет. Актуальный список покажет /keys",
        )
        return
    if key["owner_chat_id"] == api_keys.SHARED_CHAT_ID:
        await _reply(
            update,
            context,
            f"Ключ {api_keys.mask_key(key['api_key'])} общий для всех чатов и задан в "
            "настройках бота - убрать его можно только оттуда.",
        )
        return

    api_keys.remove_key(conn, chat_id, key)
    left = len(api_keys.list_chat_keys(conn, chat_id))
    await _reply(
        update,
        context,
        f"Ключ {api_keys.mask_key(key['api_key'])} удален. Ключей в пуле: {left}.",
    )


async def rotate_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принудительно переключает чат на следующий пригодный ключ.

    :param update: объект обновления Telegram
    :type update: Update
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    """
    chat_id = update.effective_chat.id
    pool = _pool(context, chat_id)
    keys = pool.keys()

    if not keys:
        await _reply(update, context, "У чата нет ключей - переключаться не на что.")
        return
    if len(keys) == 1:
        await _reply(
            update,
            context,
            "В пуле всего один ключ. Добавьте второй командой /addkey, "
            "иначе переключаться некуда.",
        )
        return

    key = pool.rotate("команда /rotatekey")
    if key is None:
        await _reply(
            update,
            context,
            "Свободных ключей больше нет: остальные выбрали дневную квоту или "
            "отклонены API. Подробности покажет /keys",
        )
        return

    await _reply(
        update, context, f"Активный ключ теперь {api_keys.mask_key(key['api_key'])}."
    )


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает или меняет модель Gemini для этого чата.

    :param update: объект обновления Telegram
    :type update: Update
    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    """
    chat_id = update.effective_chat.id
    conn = context.bot_data["db_conn"]
    default_model = context.bot_data["default_model"]
    current = chat_settings.get_model(conn, chat_id, default_model)

    if not context.args:
        await _reply(update, context, await _describe_models(context, chat_id, current))
        return

    requested = context.args[0].strip().removeprefix("models/")
    if requested.lower() in DEFAULT_MODEL_ALIASES:
        chat_settings.reset_model(conn, chat_id)
        await _reply(
            update, context, f"Чат вернулся к модели по умолчанию: {default_model}"
        )
        return

    available = await _available_models(context, chat_id)
    if available and requested not in available:
        await _reply(
            update,
            context,
            f"Модели {requested} нет среди доступных этому ключу. "
            "Список покажет /model без аргументов.",
        )
        return

    chat_settings.set_model(conn, chat_id, requested)
    await _reply(update, context, f"Модель чата: {requested}")


async def _available_models(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> list[str] | None:
    """
    Возвращает список моделей, доступных активному ключу чата.

    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    :param chat_id: идентификатор чата
    :type chat_id: int
    :return: имена моделей или None, если спросить не у кого или не вышло
    :rtype: list[str] | None
    """
    pool = _pool(context, chat_id)
    try:
        key = pool.active()
    except api_keys.NoUsableKeys as e:
        logger.info("Список моделей для чата %s недоступен: %s", chat_id, e)
        return None

    try:
        return await asyncio.to_thread(list_models, key["api_key"])
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Не удалось получить список моделей: %s", e)
        return None


async def _describe_models(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, current: str
) -> str:
    """
    Собирает ответ на /model без аргументов: текущая модель и что еще доступно.

    :param context: контекст обработчика
    :type context: ContextTypes.DEFAULT_TYPE
    :param chat_id: идентификатор чата
    :type chat_id: int
    :param current: модель, на которой чат работает сейчас
    :type current: str
    :return: текст ответа
    :rtype: str
    """
    lines = [f"Модель чата: {current}", ""]
    available = await _available_models(context, chat_id)
    if available is None:
        lines.append(
            "Список доступных моделей получить не удалось - нужен рабочий ключ. "
            "Имя модели можно задать и вслепую: /model ИМЯ"
        )
        return "\n".join(lines)

    lines.append("Доступны:")
    lines.extend(f"• {name}" for name in available)
    lines.append("")
    lines.append("Переключиться: /model ИМЯ")
    return "\n".join(lines)
