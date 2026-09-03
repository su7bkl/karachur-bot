"""
Files API: выгрузка медиа на сторону Gemini и учет уже выгруженного.

Байтами файл в запрос не попадает - в contents уходит ссылка на него, а сам файл заранее
кладется в Files API. Выгрузка не мгновенная: пока Google разбирает файл, тот висит в
состоянии PROCESSING, и ссылка на него бесполезна. Поэтому upload_file ждет ACTIVE прямо
в теле функции, обычным sleep, - и звать его, как и все отсюда, надо через
asyncio.to_thread, иначе встанет весь бот.

Выгруженное живет на стороне Google само по себе и переживает не один запрос, так что
второй раз тот же файл не грузим. Но вечным оно не бывает: Files API держит файл 48
часов, а потом удаляет. Смена ключа сюда не относится: она видна прямо в ключе кэша, и
файл просто выгружается заново от имени нового ключа.

Кэш поэтому помнит не только сам объект File, но и срок его жизни, и работает по трем
правилам.

Первое: пока до срока далеко, записи верим на слово и в сеть не ходим. Раньше состояние
у Files API спрашивали перед КАЖДОЙ отправкой каждого файла, а история строится заново
на каждый запрос, на каждое сжатие контекста и на каждую ротацию ключа - чат с тремя
десятками вложений успевал сделать под сотню последовательных сетевых вызовов, прежде
чем задать модели вопрос.

Второе: просроченное выбрасывается само, без всякого обращения к API. Спрашивать про
файл, которого по нашим же данным уже нет, незачем - его сразу выгружают заново.

Третье: у самого срока (и по требованию извне - recheck_uploads) записи все-таки
перепроверяются у API. Наш срок бывает не точен: если Files API не назвал expiration_time,
он взят по документации, а не от Google.

Зачем такая возня: ссылка на протухшую выгрузку - это не безобидный промах. Gemini
отвечает на нее "403 PERMISSION_DENIED ... access the File ...", а бот до недавнего
времени читал всякий 403 как отказ по ключу и помечал ключ негодным навсегда. Дальше он
брал следующий ключ, отправлял ту же мертвую ссылку и хоронил и его: одна старая
картинка в истории способна была выкосить весь пул чата. Разбор ошибки чинится в
karachur.gemini.errors, но сама причина - несвежий кэш - лечится здесь.
"""

import dataclasses
import datetime
import logging
import time

from google import genai

logger = logging.getLogger(__name__)

# Сколько живет выгрузка, если Files API не сказал этого сам. В документации Gemini срок
# один и тот же для всех файлов - 48 часов; на него и опираемся, когда в ответе нет
# expiration_time.
DEFAULT_UPLOAD_TTL = 48 * 60 * 60

# Запас перед сроком. Ссылку, которой осталось жить меньше этого, отправлять уже нельзя:
# пока история соберется, запрос уйдет по сети и модель до него доберется, файл успеет
# исчезнуть - и мы получим тот самый 403 про File. Дешевле выгрузить заново.
EXPIRY_MARGIN = 10 * 60

# За сколько до срока запись перестает считаться заведомо свежей и начинает
# перепроверяться у API. Нужно потому, что срок мы иногда не знаем, а предполагаем (см.
# DEFAULT_UPLOAD_TTL): у настоящей выгрузки он может оказаться и позже, и раньше нашего.
# Внутри окна на файл приходится один дешевый files.get вместо повторной выгрузки.
RECHECK_WINDOW = 2 * 60 * 60


@dataclasses.dataclass
class Upload:
    """
    Что бот помнит о выгруженном файле.

    :ivar file: объект File из google-genai - из него берутся uri и mime_type
    :ivar expires_at: unix-время, когда Files API удалит выгрузку
    :ivar suspect: запись под подозрением - перед отправкой спросить API (recheck_uploads)
    """

    file: genai.types.File
    expires_at: float
    suspect: bool = False

    def is_dead(self, now: float) -> bool:
        """
        Отвечает, поздно ли пользоваться этой выгрузкой.

        :param now: текущее unix-время
        :type now: float
        :return: True, если срок вышел или выйдет в ближайшие EXPIRY_MARGIN секунд
        :rtype: bool
        """
        return now >= self.expires_at - EXPIRY_MARGIN

    def needs_check(self, now: float) -> bool:
        """
        Отвечает, надо ли спросить у Files API, жив ли еще файл.

        :param now: текущее unix-время
        :type now: float
        :return: True, если запись под подозрением или срок уже близко
        :rtype: bool
        """
        return self.suspect or now >= self.expires_at - EXPIRY_MARGIN - RECHECK_WINDOW


# Файлы, выгруженные в Files API. Ключ кэша - пара (ключ Gemini, путь к файлу): выгрузка
# принадлежит проекту того ключа, которым ее делали, и после ротации ссылка на нее
# становится чужой. Поэтому для каждого ключа файл выгружается заново.
uploaded_files: dict[tuple[str, str], Upload] = {}


def _expires_at(uploaded_file) -> float:
    """
    Определяет, когда выгрузка перестанет существовать.

    :param uploaded_file: объект File, каким его отдал Files API
    :return: unix-время истечения срока
    :rtype: float
    """
    expiration = getattr(uploaded_file, "expiration_time", None)
    if not expiration:
        # Поле необязательное: оно заполняется, только когда файл "scheduled to expire".
        # Отсутствие срока не означает вечности - просто Google его не назвал, и мы
        # берем документированные 48 часов.
        return time.time() + DEFAULT_UPLOAD_TTL
    if expiration.tzinfo is None:
        # Files API отдает время в UTC; naive datetime без этого прочитался бы как
        # местное время, и в чужом часовом поясе срок уехал бы на часы.
        expiration = expiration.replace(tzinfo=datetime.timezone.utc)
    return expiration.timestamp()


def cached_file(api_key: str, media_path: str):
    """
    Отдает выгруженный файл из кэша, если он там есть.

    :param api_key: ключ Gemini, которым файл выгружали
    :type api_key: str
    :param media_path: путь к файлу
    :type media_path: str
    :return: объект File из google-genai или None, если файл надо выгружать
    """
    entry = uploaded_files.get((api_key, media_path))
    return entry.file if entry else None


def forget_expired() -> int:
    """
    Выбрасывает из кэша выгрузки, срок которых вышел.

    Проверяются все записи разом, а не только та, к которой обратились: истекшая запись
    чужого чата все равно ни на что не годится, а сама она о себе не напомнит - в кэш
    мимо этой функции никто не заглядывает.

    :return: сколько записей выброшено
    :rtype: int
    """
    now = time.time()
    dead = [key for key, entry in uploaded_files.items() if entry.is_dead(now)]
    for key in dead:
        del uploaded_files[key]
    if dead:
        logger.info("Из кэша выгрузок убрано %d записей с истекшим сроком.", len(dead))
    return len(dead)


def recheck_uploads(api_key: str | None = None) -> int:
    """
    Заставляет перепроверить выгрузки перед следующей отправкой.

    Зовется, когда Gemini отказала в доступе к файлу: значит, кэш разошелся с
    действительностью и верить ему на слово больше нельзя.

    Записи именно помечаются, а не выбрасываются, и это осознанный выбор. Выбросить -
    значит выгрузить заново вообще все: в чате с тремя десятками вложений это десятки
    мегабайт и минуты ожидания, притом что мертвым из них был один файл. Пометка стоит
    одного дешевого files.get на файл, после которого заново уедет ровно то, чего на
    стороне Google действительно не осталось.

    :param api_key: перепроверить выгрузки только этого ключа Gemini; None - все
    :type api_key: str | None
    :return: сколько записей помечено
    :rtype: int
    """
    marked = 0
    for (key, _), entry in uploaded_files.items():
        if api_key is None or key == api_key:
            entry.suspect = True
            marked += 1
    logger.info("Кэш выгрузок помечен на перепроверку: %d записей.", marked)
    return marked


def check_file_validity(client: genai.Client, api_key: str, media_path: str):
    """
    Готовит запись кэша к отправке: убирает протухшее, сомнительное сверяет с API.

    В сеть ходит только по делу - когда срок выгрузки на исходе или запись помечена
    подозрительной. Заведомо свежая запись не стоит ни одного вызова.

    :param client: клиент ИИ
    :type client: genai.Client
    :param api_key: ключ Gemini, которым файл выгружали
    :type api_key: str
    :param media_path: путь к файлу
    :type media_path: str
    """
    forget_expired()

    cache_key = (api_key, media_path)
    entry = uploaded_files.get(cache_key)
    if entry is None or not entry.needs_check(time.time()):
        # Записи нет вовсе (выгрузит upload_file) либо ей еще верим.
        return

    try:
        remote_file = client.files.get(name=entry.file.name)
        state = getattr(remote_file.state, "name", None)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # Здесь широкий перехват уместен: что бы ни случилось - обрыв связи, отказ SDK,
        # удаленный файл, - ответ один и безопасный: считаем выгрузку негодной и делаем
        # ее заново. Ошибку при этом никто не глотает молча, она уходит в лог.
        logger.warning(
            "Не удалось проверить статус файла %s, перевыгружаем: %s", media_path, e
        )
        del uploaded_files[cache_key]
        return

    if state != "ACTIVE":
        logger.info("Файл %s в состоянии %s, требуется перевыгрузка", media_path, state)
        del uploaded_files[cache_key]
        return

    # Ответ API свежее нашей записи: берем из него и сам объект, и срок.
    entry.file = remote_file
    entry.expires_at = _expires_at(remote_file)
    entry.suspect = False
    if entry.is_dead(time.time()):
        # Файл пока жив, но умрет раньше, чем им успеют воспользоваться.
        logger.info("Срок файла %s на исходе, перевыгружаем заранее", media_path)
        del uploaded_files[cache_key]


def upload_file(client: genai.Client, api_key: str, media_path):
    """
    Загружает файл

    :param client: клиент ИИ
    :type client: genai.Client
    :param api_key: ключ Gemini, от имени которого идет выгрузка
    :type api_key: str
    :param media_path: путь к файлу
    :type media_path: str
    """
    uploaded_file = client.files.upload(file=media_path)

    # Цикл ожидания перехода в рабочее состояние
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state.name == "ACTIVE":
        uploaded_files[(api_key, media_path)] = Upload(
            file=uploaded_file, expires_at=_expires_at(uploaded_file)
        )
    else:
        logger.error(
            "Файл %s после загрузки перешел в состояние %s",
            media_path,
            uploaded_file.state.name,
        )
