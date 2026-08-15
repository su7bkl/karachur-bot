"""
Тесты команд: ключи и выбор модели глазами человека в чате.

Проверяется в том числе то, что бот не выдает ключ целиком и убирает сообщение с
/addkey: команда доступна всем участникам, и ключ не должен оставаться в истории.
"""

import api_keys
import bot
import chat_settings
import commands
from conftest import CHAT_ONE, KEY_ONE, KEY_TWO, SHARED_KEY


def test_empty_pool_suggests_adding_a_key(run_command):
    """Пустой пул подсказывает, с чего начать."""
    assert "/addkey" in run_command.run(commands.keys_command)


def test_key_is_added_and_message_removed(run_command):
    """Ключ добавляется, а сообщение с ним исчезает из чата."""
    answer = run_command.run(commands.add_key_command, KEY_ONE)

    assert "добавлен" in answer
    assert "Ключей в пуле: 1" in answer
    assert run_command.deleted == 1


def test_added_key_is_never_echoed(run_command):
    """Ключ не возвращается в чат целиком даже в подтверждении."""
    answer = run_command.run(commands.add_key_command, KEY_ONE)

    assert KEY_ONE not in answer
    assert KEY_ONE[-4:] in answer


def test_command_without_key_still_deletes_the_message(run_command):
    """Сообщение убирается и тогда, когда ключ не разобрали."""
    answer = run_command.run(commands.add_key_command)

    assert "Формат:" in answer
    assert run_command.deleted == 1


def test_repeated_key_is_noticed(run_command):
    """Повторное добавление того же ключа не плодит дублей."""
    run_command.run(commands.add_key_command, KEY_ONE)
    answer = run_command.run(commands.add_key_command, KEY_ONE)

    assert "уже есть" in answer


def test_key_list_shows_state(run_command):
    """Список ключей показывает активный, счетчик и модель чата."""
    run_command.run(commands.add_key_command, KEY_ONE)
    run_command.run(commands.add_key_command, KEY_TWO)

    answer = run_command.run(commands.keys_command)

    assert "1. " in answer and "2. " in answer
    assert "активный" in answer
    assert "запросов сегодня: 0 из 250" in answer
    assert bot.MODEL in answer


def test_key_list_marks_the_active_key_before_first_request(run_command):
    """Активный ключ помечен сразу, не дожидаясь первого похода в API."""
    run_command.run(commands.add_key_command, KEY_ONE)

    assert "активный" in run_command.run(commands.keys_command)


def test_broken_key_shows_its_reason(run_command, db):
    """Отклоненный ключ виден в списке вместе с причиной."""
    run_command.run(commands.add_key_command, KEY_ONE)
    pool = api_keys.KeyPool(db, CHAT_ONE, bot.MODEL, 250)
    pool.mark_broken(pool.active(), "403 PERMISSION_DENIED")

    assert "отклонен API" in run_command.run(commands.keys_command)


def test_rotate_switches_the_active_key(run_command, db):
    """Ротация переключает чат на следующий ключ."""
    run_command.run(commands.add_key_command, KEY_ONE)
    run_command.run(commands.add_key_command, KEY_TWO)

    answer = run_command.run(commands.rotate_key_command)

    assert "Активный ключ теперь" in answer
    assert api_keys.KeyPool(db, CHAT_ONE, bot.MODEL, 250).active()["api_key"] == KEY_TWO


def test_rotate_needs_a_spare_key(run_command):
    """С единственным ключом ротация объясняет, чего не хватает."""
    run_command.run(commands.add_key_command, KEY_ONE)

    assert "один ключ" in run_command.run(commands.rotate_key_command)


def test_rotate_on_empty_pool(run_command):
    """Пустому пулу ротация отвечает без ошибок."""
    assert "нет ключей" in run_command.run(commands.rotate_key_command)


def test_key_is_deleted_by_tail(run_command):
    """Ключ удаляется по хвосту, который виден в списке."""
    run_command.run(commands.add_key_command, KEY_ONE)
    run_command.run(commands.add_key_command, KEY_TWO)

    answer = run_command.run(commands.delete_key_command, KEY_ONE[-6:])

    assert "удален" in answer
    assert "Ключей в пуле: 1" in answer


def test_unknown_key_is_not_deleted(run_command):
    """Ссылку на несуществующий ключ команда отвергает."""
    run_command.run(commands.add_key_command, KEY_ONE)

    assert "Такого ключа" in run_command.run(commands.delete_key_command, "99")


def test_shared_key_cannot_be_deleted_from_chat(run_command, db):
    """Общий ключ из конфига командой не убрать."""
    api_keys.sync_shared_key(db, SHARED_KEY)

    answer = run_command.run(commands.delete_key_command, "1")

    assert "только оттуда" in answer
    assert api_keys.list_chat_keys(db, CHAT_ONE, bot.MODEL)


def test_model_is_shown_with_the_available_list(run_command):
    """Команда без аргументов показывает текущую модель и что доступно."""
    run_command.run(commands.add_key_command, KEY_ONE)

    answer = run_command.run(commands.model_command)

    assert "Модель чата: gemini-2.5-flash-lite" in answer
    assert "gemini-3-pro" in answer


def test_model_is_switched(run_command, db):
    """Модель переключается и запоминается за чатом."""
    run_command.run(commands.add_key_command, KEY_ONE)

    answer = run_command.run(commands.model_command, "gemini-3-pro")

    assert answer == "Модель чата: gemini-3-pro"
    assert chat_settings.get_model(db, CHAT_ONE, "запасная") == "gemini-3-pro"


def test_model_prefix_is_trimmed(run_command, db):
    """Имя вида models/gemini-3-pro принимается наравне с коротким."""
    run_command.run(commands.add_key_command, KEY_ONE)

    run_command.run(commands.model_command, "models/gemini-3-pro")

    assert chat_settings.get_model(db, CHAT_ONE, "запасная") == "gemini-3-pro"


def test_unknown_model_is_rejected(run_command, db):
    """Опечатка в имени модели не проходит молча."""
    run_command.run(commands.add_key_command, KEY_ONE)
    run_command.run(commands.model_command, "gemini-3-pro")

    answer = run_command.run(commands.model_command, "gemini-выдуманная")

    assert "нет среди доступных" in answer
    assert chat_settings.get_model(db, CHAT_ONE, "запасная") == "gemini-3-pro"


def test_model_resets_to_default(run_command, db):
    """Сброс возвращает чат на модель из конфига."""
    run_command.run(commands.add_key_command, KEY_ONE)
    run_command.run(commands.model_command, "gemini-3-pro")

    answer = run_command.run(commands.model_command, "default")

    assert bot.MODEL in answer
    # Своей модели у чата больше нет - дальше подставляется значение из конфига.
    stored = db.execute(
        "SELECT model FROM chat_settings WHERE chat_id = ?", (CHAT_ONE,)
    ).fetchone()[0]
    assert stored is None


def test_model_without_a_key_is_still_settable(run_command, db):
    """Без рабочего ключа список не получить, но имя модели принимается вслепую."""
    answer = run_command.run(commands.model_command)
    assert "получить не удалось" in answer

    run_command.run(commands.model_command, "gemini-3-pro")
    assert chat_settings.get_model(db, CHAT_ONE, "запасная") == "gemini-3-pro"


def test_help_lists_every_command(run_command):
    """Справка перечисляет все команды бота."""
    answer = run_command.run(commands.help_command)

    for command in ("/keys", "/addkey", "/delkey", "/rotatekey", "/model", "/help"):
        assert command in answer
