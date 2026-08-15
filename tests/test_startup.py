"""
Тест запуска: main() доходит до опроса Telegram и правильно все раскладывает.

Опрос Telegram подменяется, поэтому тест никуда не ходит: проверяется только то, что
бот собрал приложение, зарегистрировал команды и привязал общий ключ из конфига.
"""

import sqlite3

from telegram.ext import Application, CommandHandler, MessageHandler

import bot
from conftest import SHARED_KEY

EXPECTED_COMMANDS = {"help", "start", "keys", "addkey", "delkey", "rotatekey", "model"}


def collect_handlers(application):
    """Разбирает зарегистрированные обработчики на команды и обычные сообщения."""
    commands_found, message_handlers = set(), 0
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                commands_found.update(handler.commands)
            elif isinstance(handler, MessageHandler):
                message_handlers += 1
    return commands_found, message_handlers


def test_main_registers_handlers_and_shared_key(tmp_path, monkeypatch):
    """Запуск создает таблицы, вешает обработчики и привязывает ключ из конфига."""
    db_path = tmp_path / "startup.db"
    monkeypatch.setattr(bot, "DB_FILE", str(db_path))
    monkeypatch.setattr(bot, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(bot, "GEMINI_API_KEY", SHARED_KEY)

    started = {}

    def fake_polling(self, *args, **kwargs):  # pylint: disable=unused-argument
        """Вместо опроса Telegram запоминает собранное приложение."""
        started["application"] = self

    monkeypatch.setattr(Application, "run_polling", fake_polling)
    bot.main()

    application = started["application"]
    commands_found, message_handlers = collect_handlers(application)

    assert commands_found == EXPECTED_COMMANDS
    assert message_handlers == 1
    assert application.bot_data["default_model"] == bot.MODEL
    assert application.bot_data["key_rpd_limit"] == bot.KEY_RPD_LIMIT

    # Свое соединение main() закрывает, выйдя из опроса, поэтому смотрим в файл заново.
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        shared = conn.execute(
            "SELECT COUNT(*) FROM chat_keys WHERE chat_id = 0"
        ).fetchone()[0]
    finally:
        conn.close()

    assert {
        "messages",
        "context_summaries",
        "api_keys",
        "chat_keys",
        "chat_settings",
    } <= tables
    assert shared == 1


def test_missing_token_stops_the_bot(monkeypatch):
    """Без токена бот не запускается и говорит почему."""
    monkeypatch.setattr(bot, "BOT_TOKEN", "")

    try:
        bot.main()
    except ValueError as error:
        assert "BOT_TOKEN" in str(error)
    else:
        raise AssertionError("бот запустился без токена")


def test_config_path_comes_from_the_environment(tmp_path, monkeypatch):
    """Путь к конфигу берется из переменной окружения."""
    config = tmp_path / "custom.cfg"
    config.write_text(
        "[SETTINGS]\nBOT_TOKEN = t\nDB_FILE = d\nMEDIA_DIR = m\n"
        "TRIGGER_WORD = w\nSYSTEM_PROMPT = p\nMODEL = custom-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(bot.CONFIG_ENV_VAR, str(config))

    settings = bot.load_config()

    assert settings["MODEL"] == "custom-model"
    # Необязательные параметры получают запасные значения.
    assert settings["GEMINI_API_KEY"] == ""
    assert settings["KEY_RPD_LIMIT"] == 250
