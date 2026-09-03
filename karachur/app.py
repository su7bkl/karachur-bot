"""
Точка входа бота: собрать настройки, открыть базу, развесить обработчики и уйти в опрос
Telegram.

Сам код разъехался по пакету karachur: разговор с моделью - в karachur.gemini, история и
ключи - в karachur.storage, работа с файлами - в karachur.media, все про Telegram -
в karachur.tg. Здесь остается только main() и список команд, которые он регистрирует.

Модуль запускают двумя способами - `python bot.py` (тонкая обертка в корне репозитория,
оставленная ради привычной команды на боевой машине) и `python -m karachur`. Оба ведут
сюда, поэтому логирование настраивается один раз здесь, при импорте модуля, а не в каждой
обертке по отдельности.
"""

import logging

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from karachur import config
from karachur.storage import keys as key_store
from karachur.storage import schema
from karachur.tg import commands, handlers

# --- НАСТРОЙКИ ---
# Настройки живут в karachur.config и собираются в main(): дальше по коду они идут
# явными аргументами, а обработчикам достаются через bot_data, а не модульными глобалами.

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ТОЧКА ВХОДА ---


# Команды бота: имя в Telegram и обработчик.
COMMAND_HANDLERS = {
    "help": commands.help_command,
    "start": commands.help_command,
    "keys": commands.keys_command,
    "addkey": commands.add_key_command,
    "delkey": commands.delete_key_command,
    "rotatekey": commands.rotate_key_command,
    "model": commands.model_command,
}


def main():
    """
    Основная функция запуска бота.

    Здесь и только здесь читается конфиг: дальше настройки идут по коду аргументами, а
    обработчикам достаются через bot_data. Поэтому импорт этого модуля сам по себе ничего
    не читает с диска и не требует существующего config.cfg.
    """
    cfg = config.load_config()

    if not cfg.bot_token:
        raise ValueError("Пожалуйста, проверьте файл конфигурации: BOT_TOKEN не указан.")

    db_connection = schema.init_db(cfg.db_file, cfg.media_dir)

    # Ключ из конфига доступен всем чатам сразу; свои чат добавляет командой /addkey.
    key_store.sync_shared_key(db_connection, cfg.gemini_api_key)
    if not cfg.gemini_api_key:
        logger.info(
            "Общий ключ в config.cfg не задан - чаты работают только на своих ключах."
        )

    # Чаты обслуживаются параллельно: ответ с повторами занимает минуты, и один чат не
    # должен становиться очередью для всех остальных. Порядок внутри чата держит замок.
    application = (
        Application.builder().token(cfg.bot_token).concurrent_updates(True).build()
    )

    application.bot_data["db_conn"] = db_connection
    application.bot_data["cfg"] = cfg

    for name, handler in COMMAND_HANDLERS.items():
        application.add_handler(CommandHandler(name, handler))

    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handlers.handle_message)
    )

    logger.info(
        "Модель по умолчанию: %s. Потолок запросов на ключ в сутки: %s.",
        cfg.model,
        cfg.key_rpd_limit or "не задан",
    )
    logger.info("Бот запускается...")
    application.run_polling()

    db_connection.close()
    logger.info("Соединение с БД закрыто.")
