"""
Общая оснастка тестов: настройки, база и подделки Gemini с Telegram.

Настройки собираются фикстурой cfg прямо в памяти: рабочий config.cfg тесты не трогают
и даже не требуют - на чистой машине с одним склонированным репозиторием они все равно
проходят. Раньше на это уходил временный config.cfg на диске, потому что bot.py читал
конфиг на импорте; теперь конфиг читает только main(), и подкладывать файл незачем.

Ни один тест не ходит в сеть: клиент Gemini подменяется подделкой, которая отвечает по
заранее заданному сценарию, а Telegram сводится к паре объектов, запоминающих отправку.
"""

# Подделки повторяют форму настоящих объектов Gemini и Telegram: отсюда классы с одним
# методом и аргументы вроде model, которые тесту не нужны, но есть в исходной сигнатуре.
# Атрибутов у FakeGemini много по той же причине: она не только отвечает по сценарию, но
# и ведет журналы всего, о чем ее спросили, - на них и держатся проверки тестов.
# pylint: disable=too-few-public-methods,unused-argument,too-many-instance-attributes

import asyncio
import datetime
import types

import pytest

from karachur import config
from karachur.gemini import files

# Модуль зовется key_pool, а не pool: имя pool в тестах занято самим пулом чата.
from karachur.gemini import pool as key_pool
from karachur.storage import schema
from karachur.tg import commands

# Ключи в тестах намеренно непохожи на настоящие, но той же длины и формы.
KEY_ONE = "AIzaTEST0000000000000000000000000000001"
KEY_TWO = "AIzaTEST0000000000000000000000000000002"
KEY_THREE = "AIzaTEST0000000000000000000000000000003"
SHARED_KEY = "AIzaSHARED000000000000000000000000000001"

CHAT_ONE = -1001110000
CHAT_TWO = -1002220000

# Модель тестовых настроек. Квоты считаются на пару "ключ и модель", поэтому почти
# всякая работа с пулом требует назвать модель.
MODEL = "gemini-2.5-flash-lite"
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

    @classmethod
    def stale_file(cls, name: str = "files/protuhla") -> "ApiError":
        """
        Возвращает отказ по ссылке на файл - слово в слово как настоящий.

        Код тот же 403, что и у отвергнутого ключа: на этом сходстве бот и хоронил
        собственные ключи за чужую вину.

        :param name: имя ресурса в Files API
        :type name: str
        :return: ошибка про недоступный файл
        :rtype: ApiError
        """
        return cls(
            403,
            "PERMISSION_DENIED. You do not have permission to access the File "
            f"{name} or it may not exist.",
        )


class FakeFile:
    """
    Объект File из Files API: кэшу выгрузок нужны имя, ссылка, mime, состояние и срок.

    Срок задается смещением от текущего момента - так тест говорит "выгрузка протухла
    час назад" или "живет еще сутки", не подменяя часы.
    """

    def __init__(
        self,
        name: str = "files/test",
        mime_type: str = "image/png",
        state: str = "ACTIVE",
        expires_in: float | None = 48 * 60 * 60,
    ):
        """
        :param name: имя ресурса в Files API
        :type name: str
        :param mime_type: mime выгруженного файла
        :type mime_type: str
        :param state: состояние выгрузки - ACTIVE, PROCESSING или FAILED
        :type state: str
        :param expires_in: через сколько секунд Files API удалит файл; None - срок не
            назван, как у настоящего File без expiration_time
        :type expires_in: float | None
        """
        self.name = name
        self.uri = f"https://files.example/{name}"
        self.mime_type = mime_type
        self.state = types.SimpleNamespace(name=state)
        self.expiration_time = None
        if expires_in is not None:
            self.expiration_time = datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(seconds=expires_in)


class _FakeFiles:
    """
    Подделка client.files: помнит, что лежит на стороне Google, и считает обращения.

    Счет обращений тут не для красоты: главное свойство кэша выгрузок - не спрашивать
    Files API попусту, а доказывается оно только счетчиком.
    """

    def __init__(self, gemini: "FakeGemini"):
        """
        :param gemini: общий держатель состояния подделки
        :type gemini: FakeGemini
        """
        self.gemini = gemini

    def get(self, name):
        """
        Отдает выгрузку, если она еще существует.

        :param name: имя ресурса в Files API
        :return: поддельный File
        :rtype: FakeFile
        :raises ApiError: если файла на стороне Google нет
        """
        self.gemini.file_gets.append(name)
        remote = self.gemini.remote_files.get(name)
        if remote is None:
            raise ApiError.stale_file(name)
        return remote

    def upload(self, file):
        """
        Выгружает файл и запоминает его как существующий на стороне Google.

        :param file: путь к файлу на диске
        :return: поддельный File
        :rtype: FakeFile
        """
        self.gemini.file_uploads.append(file)
        remote = self.gemini.next_upload or FakeFile(
            name=f"files/upload{len(self.gemini.file_uploads)}"
        )
        self.gemini.next_upload = None
        self.gemini.remote_files[remote.name] = remote
        return remote


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
        self.files = _FakeFiles(gemini)


class FakeGemini:
    """
    Gemini без сети: отвечает по сценарию и помнит, кого и чем звали.

    Сценарий на ключ - список шагов. Строка означает успешный ответ, исключение -
    ошибку с этой попытки.

    Files API живет в тех же объектах: remote_files - то, что якобы лежит на стороне
    Google, file_gets и file_uploads - обращения к нему.
    """

    def __init__(self):
        self.scripts = {}
        self.token_counts = []
        self.calls = []
        self.models_used = []
        self.contents_sent = []
        self.built_for = []
        # Что лежит в Files API: имя ресурса - объект FakeFile.
        self.remote_files = {}
        # Имена, о состоянии которых спрашивали, и пути, которые выгружали.
        self.file_gets = []
        self.file_uploads = []
        # Чем ответить на следующую выгрузку, если тесту нужен особенный файл.
        self.next_upload = None

    def publish(self, remote: FakeFile) -> FakeFile:
        """
        Кладет готовый файл в Files API, минуя выгрузку.

        :param remote: поддельный File
        :type remote: FakeFile
        :return: он же, чтобы тест мог сослаться на него дальше
        :rtype: FakeFile
        """
        self.remote_files[remote.name] = remote
        return remote

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

    def __init__(self, args: list, runner: "CommandRunner"):
        """
        :param args: аргументы команды
        :type args: list
        :param runner: оснастка команд - у нее же берутся база и настройки
        :type runner: CommandRunner
        """
        self.bot = _RecordingBot(runner)
        self.args = args
        self.bot_data = {"db_conn": runner.conn, "cfg": runner.cfg}


class CommandRunner:
    """Гоняет обработчики команд на поддельном контексте Telegram."""

    def __init__(self, conn, cfg: config.Config, chat_id: int = CHAT_ONE):
        """
        :param conn: соединение с базой
        :param cfg: настройки бота, которые обработчики найдут в bot_data
        :type cfg: config.Config
        :param chat_id: чат, от имени которого идут команды
        :type chat_id: int
        """
        self.conn = conn
        self.cfg = cfg
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
        context = _FakeContext(list(args), self)
        asyncio.run(handler(update, context))
        return self.sent[-1][1] if self.sent else ""


@pytest.fixture(name="cfg")
def cfg_fixture(tmp_path):
    """
    Настройки тестового бота: своя база и свой каталог медиа на каждый тест.

    Паузы между попытками укорочены до неразличимых: ждать настоящие две, четыре и
    восемь секунд тесту незачем, а зависший сценарий должен падать быстро.

    :param tmp_path: временная директория теста
    :return: настройки, с которыми работают остальные фикстуры
    :rtype: config.Config
    """
    return config.Config(
        bot_token="123456:TEST",
        db_file=str(tmp_path / "test.db"),
        media_dir=str(tmp_path / "media"),
        trigger_word="Карачур",
        system_prompt="Тестовый системный промпт.",
        model=MODEL,
        max_retries=4,
        retry_base_delay=0.01,
        retry_max_delay=0.05,
    )


@pytest.fixture(name="db")
def db_fixture(cfg):  # pylint: disable=redefined-outer-name
    """
    Свежая база со схемой бота, своя на каждый тест.

    :param cfg: настройки теста - из них берутся пути к базе и каталогу медиа
    :return: открытое соединение с базой
    """
    conn = schema.init_db(cfg.db_file, cfg.media_dir)
    yield conn
    conn.close()


@pytest.fixture(name="gemini")
def gemini_fixture(monkeypatch):
    """
    Подменяет клиента Gemini подделкой.

    Подменяется именно атрибут модуля karachur.gemini.pool: и бот, и сам пул зовут
    client_for_key через модуль, а не по импортированному имени, поэтому подмена
    работает для всех, кто им пользуется. Если где-то появится
    "from karachur.gemini.pool import client_for_key", это имя будет указывать на
    настоящую функцию, подмена его не достанет, и тест молча уйдет в сеть.

    :param monkeypatch: штатная подмена атрибутов pytest
    :return: держатель сценариев ответов
    :rtype: FakeGemini
    """
    fake = FakeGemini()
    monkeypatch.setattr(key_pool, "client_for_key", fake.client_for_key)
    return fake


@pytest.fixture(name="uploads_cache", autouse=True)
def uploads_cache_fixture():
    """
    Держит кэш выгрузок Files API пустым на входе в каждый тест и на выходе из него.

    Кэш - глобальный словарь модуля, и таким он и должен быть: его смысл в том, чтобы
    переживать запросы. Но переживать чужие тесты он не должен, поэтому фикстура
    автоматическая - иначе один тест, положивший туда выгрузку, менял бы поведение
    соседнего.

    :return: сам словарь кэша
    :rtype: dict
    """
    files.uploaded_files.clear()
    yield files.uploaded_files
    files.uploaded_files.clear()


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
def run_command_fixture(db, cfg, monkeypatch):  # pylint: disable=redefined-outer-name
    """
    Отдает оснастку для команд с подмененным списком моделей.

    Список моделей команда берет у API - в тестах он задан заранее, чтобы не ходить
    в сеть и не зависеть от того, что Google выкатил сегодня.

    :param db: соединение с базой
    :param cfg: настройки теста - обработчики найдут их в bot_data
    :param monkeypatch: штатная подмена атрибутов pytest
    :return: оснастка запуска команд
    :rtype: CommandRunner
    """
    monkeypatch.setattr(commands, "list_models", lambda api_key: [MODEL, OTHER_MODEL])
    return CommandRunner(db, cfg)
