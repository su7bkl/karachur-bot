"""
Общая оснастка тестов: конфиг, база и подделки Gemini с Telegram.

bot.py читает конфиг на импорте, поэтому тестовый конфиг готовится прямо здесь, до
всяких фикстур: conftest.py pytest импортирует раньше самих тестов. Путь передается
через KARACHUR_CONFIG, так что рабочий config.cfg тесты не трогают и даже не требуют -
на чистой машине с одним склонированным репозиторием они все равно проходят.

Ни один тест не ходит в сеть: клиент Gemini подменяется подделкой, которая отвечает по
заранее заданному сценарию, а Telegram сводится к паре объектов, запоминающих отправку.
"""

# Подделки повторяют форму настоящих объектов Gemini и Telegram: отсюда классы с одним
# методом и аргументы вроде model, которые тесту не нужны, но есть в исходной сигнатуре.
# pylint: disable=too-few-public-methods,unused-argument

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

TEST_CONFIG = """[SETTINGS]
BOT_TOKEN = 123456:TEST
GEMINI_API_KEY =
DB_FILE = tests.db
MEDIA_DIR = media
TRIGGER_WORD = Карачур
MODEL = gemini-2.5-flash-lite
MAX_CONTEXT_TOKENS = 200000
KEY_RPD_LIMIT = 250
SYSTEM_PROMPT = Тестовый системный промпт.
"""

_CONFIG_PATH = Path(tempfile.mkdtemp(prefix="karachur-tests-")) / "config.cfg"
_CONFIG_PATH.write_text(TEST_CONFIG, encoding="utf-8")
os.environ["KARACHUR_CONFIG"] = str(_CONFIG_PATH)

# Модули бота импортируются только после того, как конфиг оказался на месте.
# pylint: disable=wrong-import-position
import api_keys  # noqa: E402
import bot  # noqa: E402
import commands  # noqa: E402

# Ключи в тестах намеренно непохожи на настоящие, но той же длины и формы.
KEY_ONE = "AIzaTEST0000000000000000000000000000001"
KEY_TWO = "AIzaTEST0000000000000000000000000000002"
KEY_THREE = "AIzaTEST0000000000000000000000000000003"
SHARED_KEY = "AIzaSHARED000000000000000000000000000001"

CHAT_ONE = -1001110000
CHAT_TWO = -1002220000

# Модель из тестового конфига. Квоты считаются на пару "ключ и модель", поэтому почти
# всякая работа с пулом требует назвать модель.
MODEL = bot.MODEL
OTHER_MODEL = "gemini-3-pro"


class ApiError(Exception):
    """
    Ошибка Gemini API с кодом - в таком виде их отдает SDK.

    Разбор ошибок смотрит и на код, и на текст, поэтому подделка несет оба.
    """

    def __init__(self, code: int, message: str):
        """
        :param code: HTTP-код ответа
        :type code: int
        :param message: текст ошибки вместе с деталями квоты
        :type message: str
        """
        super().__init__(f"{code} {message}")
        self.code = code

    @classmethod
    def daily_quota(cls) -> "ApiError":
        """Возвращает 429 с выбранной дневной квотой."""
        return cls(
            429,
            "RESOURCE_EXHAUSTED quotaId: "
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        )

    @classmethod
    def rate_limit(cls) -> "ApiError":
        """Возвращает 429 с минутным лимитом и рекомендованной паузой."""
        return cls(
            429,
            "RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerMinutePerProject "
            'retryDelay: "0.01s"',
        )

    @classmethod
    def bad_key(cls) -> "ApiError":
        """Возвращает отказ по ключу."""
        return cls(403, "PERMISSION_DENIED: API key expired")


class _FakeResponse:
    """Ответ модели: разбору достаточно текста и отсутствия блокировки."""

    def __init__(self, text: str):
        """
        :param text: текст, который якобы вернула модель
        :type text: str
        """
        self.text = text
        self.prompt_feedback = None
        self.candidates = []


class _FakeTokenCount:
    """Ответ count_tokens."""

    def __init__(self, total: int):
        """
        :param total: сколько токенов якобы занимает запрос
        :type total: int
        """
        self.total_tokens = total


class _FakeModels:
    """Подделка client.models: отвечает по сценарию, заведенному на ключ."""

    def __init__(self, api_key: str, gemini: "FakeGemini"):
        """
        :param api_key: ключ, от имени которого работает клиент
        :type api_key: str
        :param gemini: общий на все ключи держатель сценариев
        :type gemini: FakeGemini
        """
        self.api_key = api_key
        self.gemini = gemini

    def generate_content(self, model, contents):
        """
        Отдает очередной шаг сценария этого ключа.

        :param model: имя модели - запоминается, чтобы тест мог его проверить
        :param contents: содержимое запроса
        :return: поддельный ответ модели
        :raises Exception: если очередным шагом сценария задана ошибка
        """
        self.gemini.calls.append(self.api_key)
        self.gemini.models_used.append(model)
        self.gemini.contents_sent.append(contents)

        steps = self.gemini.scripts.get(self.api_key)
        if not steps:
            # Иначе незапланированный вызов утонул бы в повторах вместо внятного падения.
            raise AssertionError(f"сценарий для ключа ...{self.api_key[-4:]} исчерпан")

        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return _FakeResponse(step)

    def count_tokens(self, model, contents):
        """
        Отдает заранее подготовленный размер контекста.

        :param model: имя модели
        :param contents: содержимое запроса
        :return: поддельный ответ count_tokens
        """
        if not self.gemini.token_counts:
            return _FakeTokenCount(0)
        return _FakeTokenCount(self.gemini.token_counts.pop(0))


class _FakeClient:
    """Подделка genai.Client."""

    def __init__(self, api_key: str, gemini: "FakeGemini"):
        """
        :param api_key: ключ клиента
        :type api_key: str
        :param gemini: держатель сценариев
        :type gemini: FakeGemini
        """
        self.models = _FakeModels(api_key, gemini)


class FakeGemini:
    """
    Gemini без сети: отвечает по сценарию и помнит, кого и чем звали.

    Сценарий на ключ - список шагов. Строка означает успешный ответ, исключение -
    ошибку с этой попытки.
    """

    def __init__(self):
        self.scripts = {}
        self.token_counts = []
        self.calls = []
        self.models_used = []
        self.contents_sent = []
        self.built_for = []

    def script(self, api_key: str, *steps):
        """
        Задает сценарий ответов для ключа.

        :param api_key: ключ Gemini
        :type api_key: str
        :param steps: шаги сценария по порядку
        """
        self.scripts[api_key] = list(steps)

    def client_for_key(self, api_key: str) -> _FakeClient:
        """
        Отдает поддельного клиента вместо genai.Client.

        :param api_key: ключ Gemini
        :type api_key: str
        :return: поддельный клиент
        :rtype: _FakeClient
        """
        return _FakeClient(api_key, self)

    async def make_contents(self, key: dict) -> list:
        """
        Собирает запрос под ключ и запоминает, для какого ключа его собирали.

        :param key: строка ключа из пула
        :type key: dict
        :return: содержимое запроса
        :rtype: list
        """
        self.built_for.append(key["api_key"])
        return [f"запрос под ...{key['api_key'][-4:]}"]


class _RecordingBot:
    """Подделка telegram.Bot: только запоминает отправленное."""

    def __init__(self, runner: "CommandRunner"):
        """
        :param runner: оснастка, куда складывать отправленное
        :type runner: CommandRunner
        """
        self.runner = runner

    async def send_message(self, chat_id, text):
        """
        Запоминает ответ бота вместо отправки в Telegram.

        :param chat_id: чат назначения
        :param text: текст ответа
        """
        self.runner.sent.append((chat_id, text))


class _RecordingMessage:
    """Подделка сообщения с командой: помнит, убрали ли его из чата."""

    def __init__(self, runner: "CommandRunner"):
        """
        :param runner: оснастка, где ведется счет удалений
        :type runner: CommandRunner
        """
        self.runner = runner

    async def delete(self):
        """Отмечает, что сообщение с командой удалено."""
        self.runner.deleted += 1


class _FakeUpdate:
    """Обновление Telegram с полями, которые читают обработчики команд."""

    def __init__(self, chat_id: int, runner: "CommandRunner"):
        """
        :param chat_id: идентификатор чата
        :type chat_id: int
        :param runner: оснастка команд
        :type runner: CommandRunner
        """
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.effective_message = _RecordingMessage(runner)


class _FakeContext:
    """Контекст обработчика с общими данными бота."""

    def __init__(self, conn, args: list, runner: "CommandRunner"):
        """
        :param conn: соединение с базой
        :param args: аргументы команды
        :type args: list
        :param runner: оснастка команд
        :type runner: CommandRunner
        """
        self.bot = _RecordingBot(runner)
        self.args = args
        self.bot_data = {
            "db_conn": conn,
            "default_model": bot.MODEL,
            "key_rpd_limit": bot.KEY_RPD_LIMIT,
        }


class CommandRunner:
    """Гоняет обработчики команд на поддельном контексте Telegram."""

    def __init__(self, conn, chat_id: int = CHAT_ONE):
        """
        :param conn: соединение с базой
        :param chat_id: чат, от имени которого идут команды
        :type chat_id: int
        """
        self.conn = conn
        self.chat_id = chat_id
        self.sent = []
        self.deleted = 0

    def run(self, handler, *args) -> str:
        """
        Зовет обработчик команды и возвращает последний ответ в чат.

        :param handler: обработчик из commands
        :param args: аргументы команды, как их разобрал бы Telegram
        :return: текст последнего ответа или пустая строка, если бот промолчал
        :rtype: str
        """
        self.sent = []
        update = _FakeUpdate(self.chat_id, self)
        context = _FakeContext(self.conn, list(args), self)
        asyncio.run(handler(update, context))
        return self.sent[-1][1] if self.sent else ""


@pytest.fixture(name="db")
def db_fixture(tmp_path, monkeypatch):
    """
    Свежая база со схемой бота, своя на каждый тест.

    :param tmp_path: временная директория теста
    :param monkeypatch: штатная подмена атрибутов pytest
    :return: открытое соединение с базой
    """
    monkeypatch.setattr(bot, "DB_FILE", str(tmp_path / "test.db"))
    monkeypatch.setattr(bot, "MEDIA_DIR", str(tmp_path / "media"))
    conn = bot.init_db()
    yield conn
    conn.close()


@pytest.fixture(name="gemini")
def gemini_fixture(monkeypatch):
    """
    Подменяет клиента Gemini подделкой и укорачивает паузы между попытками.

    :param monkeypatch: штатная подмена атрибутов pytest
    :return: держатель сценариев ответов
    :rtype: FakeGemini
    """
    fake = FakeGemini()
    monkeypatch.setattr(api_keys, "client_for_key", fake.client_for_key)
    # Ждать настоящие паузы незачем, а зависший сценарий должен падать быстро.
    monkeypatch.setattr(bot, "MAX_RETRIES", 4)
    monkeypatch.setattr(bot, "RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr(bot, "RETRY_MAX_DELAY", 0.05)
    return fake


@pytest.fixture(name="api_error")
def api_error_fixture():
    """
    Отдает фабрику ошибок API.

    :return: класс поддельной ошибки Gemini
    :rtype: type[ApiError]
    """
    return ApiError


@pytest.fixture(name="add_message")
def add_message_fixture(db):  # pylint: disable=redefined-outer-name
    """
    Отдает функцию, кладущую сообщение в базу в обход объекта Telegram.

    :param db: соединение с базой
    :return: функция добавления сообщения
    """

    def _add(chat_id, message_id, content, reply_to=None, is_bot=0):
        """Кладет одно сообщение в историю чата."""
        db.execute(
            """
            INSERT OR REPLACE INTO messages
                (message_id, chat_id, user_id, username, content, timestamp,
                 reply_to_message_id, is_bot, summarized)
            VALUES (?, ?, 1, 'tester', ?, ?, ?, ?, 0)
            """,
            (
                message_id,
                chat_id,
                content,
                f"2026-08-15T10:00:{message_id % 60:02d}",
                reply_to,
                is_bot,
            ),
        )
        db.commit()

    return _add


@pytest.fixture(name="run_command")
def run_command_fixture(db, monkeypatch):  # pylint: disable=redefined-outer-name
    """
    Отдает оснастку для команд с подмененным списком моделей.

    Список моделей команда берет у API - в тестах он задан заранее, чтобы не ходить
    в сеть и не зависеть от того, что Google выкатил сегодня.

    :param db: соединение с базой
    :param monkeypatch: штатная подмена атрибутов pytest
    :return: оснастка запуска команд
    :rtype: CommandRunner
    """
    monkeypatch.setattr(
        commands, "list_models", lambda api_key: ["gemini-2.5-flash-lite", "gemini-3-pro"]
    )
    return CommandRunner(db)
