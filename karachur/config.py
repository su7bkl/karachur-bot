"""
Настройки бота: разбор config.cfg и один объект, который дальше передается явно.

Раньше настройки жили модульными глобалами bot.py и читались прямо на импорте. Из-за
этого bot.py нельзя было разрезать на модули: любой выделенный кусок начинал импортировать
константы из bot.py, а bot.py - этот кусок. Заодно импорт бота требовал существующего
config.cfg, поэтому тестам приходилось выкладывать свой файл на диск до всяких фикстур.

Теперь настройки - обычный объект: он собирается в main() и оттуда расходится по коду.
Кому что нужно, видно по сигнатурам, а тест просто строит свой Config в памяти.
"""

import configparser
import os
from dataclasses import dataclass

# Путь к конфигу можно задать переменной окружения: бота запускают и не из его папки,
# а тестам нужен свой конфиг, не трогающий рабочий.
CONFIG_ENV_VAR = "KARACHUR_CONFIG"


# Полей заметно больше семи, и это нормально: объект настроек и есть мешок значений.
# Дробить его на подобъекты ради счетчика pylint - только запутывать читателя.
@dataclass(frozen=True)
class Config:  # pylint: disable=too-many-instance-attributes
    """
    Полный набор настроек бота.

    Замороженный намеренно: настройки читаются один раз на старте, и менять их по ходу
    работы некому. Такой объект можно раздать хоть всем обработчикам сразу, не опасаясь,
    что кто-то по дороге поправит его под себя.

    Поля идут тремя группами: обязательное из config.cfg (без него бот не взлетит),
    необязательное из config.cfg (у старых конфигов этих строк нет) и то, что в конфиге
    не читается вовсе. Последнее - настройки поведения, которые незачем показывать
    человеку, но нужно уметь подменить в тесте: иначе он будет честно ждать настоящие
    паузы между попытками и упираться в настоящий лимит контекста.
    """

    # --- ОБЯЗАТЕЛЬНОЕ ИЗ CONFIG.CFG ---
    bot_token: str
    db_file: str
    media_dir: str
    trigger_word: str
    system_prompt: str
    model: str

    # --- НЕОБЯЗАТЕЛЬНОЕ ИЗ CONFIG.CFG ---
    # Ключ из конфига необязателен: чат может обойтись своими, добавленными /addkey.
    gemini_api_key: str = ""
    max_context_tokens: int = 200_000
    key_rpd_limit: int = 250

    # --- ПОВТОРНЫЕ ПОПЫТКИ ---
    # Сколько раз пробуем получить от модели корректный текст, прежде чем сдаться.
    max_retries: int = 15
    # Пауза растет экспоненциально (2, 4, 8, ...) до потолка. Суммарно ~18 минут.
    retry_base_delay: float = 2.0
    retry_max_delay: float = 120.0

    # --- СЖАТИЕ КОНТЕКСТА ---
    # Когда история перестает влезать в max_context_tokens, самая старая ее часть уходит
    # модели на пересказ, а пересказ занимает ее место в контексте следующих запросов.
    # Сжимаем с запасом: если целиться ровно в лимит, сжатие будет срабатывать почти на
    # каждое сообщение. Доля от лимита, в которую хотим уложиться после сжатия.
    context_target_ratio: float = 0.5
    # Столько последних сообщений остаются в контексте дословно при любом сжатии.
    keep_recent_messages: int = 10
    # Больше этого числа проходов сжатия за один ответ не делаем.
    max_compression_rounds: int = 3
    # Точный подсчет токенов - лишний запрос к API, поэтому сначала прикидываем размер на
    # глаз и зовем count_tokens, только если грубая оценка подобралась к этой доле лимита.
    token_check_ratio: float = 0.5
    # Кириллица в токенайзере Gemini дает примерно 2-3 символа на токен, берем нижнюю границу.
    chars_per_token: float = 2.0
    # Медиа в оценке считаем по верхней границе: недооценка дороже лишнего точного подсчета.
    media_token_estimate: int = 2000

    # --- ВЫГРУЗКА ФАЙЛОВ ---
    file_upload_delay_per_mb: float = 0.6


def load_config(config_path: str | None = None) -> Config:
    """
    Загружает настройки из конфигурационного файла (UTF-8).

    :param config_path: путь к файлу конфигурации; если не задан, берется из переменной
        окружения KARACHUR_CONFIG, а по умолчанию - config.cfg рядом с ботом
    :type config_path: str | None
    :return: собранные настройки
    :rtype: Config
    :raises FileNotFoundError: если файла по вычисленному пути нет
    """
    if config_path is None:
        config_path = os.environ.get(CONFIG_ENV_VAR, "config.cfg")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Файл конфигурации не найден: {config_path}")

    parser = configparser.ConfigParser()
    with open(config_path, "r", encoding="utf-8") as f:
        parser.read_file(f)

    return Config(
        bot_token=parser.get("SETTINGS", "BOT_TOKEN"),
        db_file=parser.get("SETTINGS", "DB_FILE"),
        media_dir=parser.get("SETTINGS", "MEDIA_DIR"),
        trigger_word=parser.get("SETTINGS", "TRIGGER_WORD"),
        system_prompt=parser.get("SETTINGS", "SYSTEM_PROMPT"),
        model=parser.get("SETTINGS", "MODEL"),
        # Необязательные параметры: у старых конфигов их нет, поэтому с запасными
        # значениями. Запасное берем у самого Config, чтобы оно не разъехалось с полем.
        gemini_api_key=parser.get(
            "SETTINGS", "GEMINI_API_KEY", fallback=Config.gemini_api_key
        ).strip(),
        max_context_tokens=parser.getint(
            "SETTINGS", "MAX_CONTEXT_TOKENS", fallback=Config.max_context_tokens
        ),
        key_rpd_limit=parser.getint(
            "SETTINGS", "KEY_RPD_LIMIT", fallback=Config.key_rpd_limit
        ),
    )
