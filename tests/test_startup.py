"""
Тест запуска: main() доходит до опроса Telegram и правильно все раскладывает.

Опрос Telegram подменяется, поэтому тест никуда не ходит: проверяется только то, что
бот собрал приложение, зарегистрировал команды и привязал общий ключ из конфига.

Конфиг main() читает сама, поэтому тесту достаточно подменить load_config - файл на
диск выкладывает только тот тест, который как раз про поиск этого файла.

main() живет в karachur.app - корневой bot.py лишь зовет ее, ничего не добавляя, поэтому
тест обращается прямо к app и саму обертку отдельно не проверяет.
"""

import dataclasses
import sqlite3

from telegram.ext import Application, CommandHandler, MessageHandler

from conftest import SHARED_KEY
from karachur import app, config

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


def test_main_registers_handlers_and_shared_key(cfg, monkeypatch):
    """Запуск создает таблицы, вешает обработчики и привязывает ключ из конфига."""
    started_cfg = dataclasses.replace(cfg, gemini_api_key=SHARED_KEY)
    monkeypatch.setattr(config, "load_config", lambda: started_cfg)

    started = {}

    def fake_polling(self, *args, **kwargs):  # pylint: disable=unused-argument
        """Вместо опроса Telegram запоминает собранное приложение."""
        started["application"] = self

    monkeypatch.setattr(Application, "run_polling", fake_polling)
    app.main()

    application = started["application"]
    commands_found, message_handlers = collect_handlers(application)

    assert commands_found == EXPECTED_COMMANDS
    assert message_handlers == 1
    # Настройки уезжают обработчикам целиком: модель и потолок запросов они берут оттуда.
    assert application.bot_data["cfg"] is started_cfg

    # Свое соединение main() закрывает, выйдя из опроса, поэтому смотрим в файл заново.
    conn = sqlite3.connect(started_cfg.db_file)
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


def test_missing_token_stops_the_bot(cfg, monkeypatch):
    """Без токена бот не запускается и говорит почему."""
    monkeypatch.setattr(
        config, "load_config", lambda: dataclasses.replace(cfg, bot_token="")
    )

    try:
        app.main()
    except ValueError as error:
        assert "BOT_TOKEN" in str(error)
    else:
        raise AssertionError("бот запустился без токена")


def test_config_path_comes_from_the_environment(tmp_path, monkeypatch):
    """Путь к конфигу берется из переменной окружения."""
    config_file = tmp_path / "custom.cfg"
    config_file.write_text(
        "[SETTINGS]\nBOT_TOKEN = t\nDB_FILE = d\nMEDIA_DIR = m\n"
        "TRIGGER_WORD = w\nSYSTEM_PROMPT = p\nMODEL = custom-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(config_file))

    settings = config.load_config()

    assert settings.model == "custom-model"
    # Необязательные параметры получают запасные значения.
    assert settings.gemini_api_key == ""
    assert settings.key_rpd_limit == 250
