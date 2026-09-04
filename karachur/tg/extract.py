"""
Разбор вложений входящего сообщения Telegram: что за файл пришел и под каким file_id он
лежит в Telegram.

Вложение в объекте telegram.Message лежит в одном из десятка отдельных полей (photo,
document, sticker...), и в конкретном сообщении заполнено не больше одного из них - какое
именно, определяет тип вложения. save_message_to_db раньше перебирала эти поля цепочкой
if/elif прямо на месте; здесь тот же перебор оформлен таблицей (karachur.storage.messages
зовет только extract_attachment). Новый тип вложения - это новая строка таблицы, а не
вклинивание в середину ветвления.

Скачивание самого файла и его перекодирование сюда не входят - только то, что можно узнать
по одному объекту сообщения, без похода в Telegram за содержимым.
"""

from dataclasses import dataclass, replace

from telegram import Message

from karachur.text import notes


@dataclass(frozen=True)
class Attachment:
    """
    Вложение сообщения, каким его нужно записать в таблицу messages.

    file_name заполнен только у document, animation, video и audio - у остальных типов
    Telegram имени файла просто не присылает. content_override заполнен только у voice и
    video_note: Telegram не расшифровывает голосовые и кружки в текст, и вместо содержимого
    реплики в базу должна уйти пометка "кто это записал" (см. save_message_to_db).
    """

    media_type: str | None
    mime_type: str | None
    file_id: str | None
    file_name: str | None
    content_override: str | None


# Датакласс заморожен, а "нет вложения" нужно возвращать в каждом сообщении без медиа -
# проще завести одну общую константу, чем на каждый вызов собирать Attachment(None, None,
# None, None, None) заново.
EMPTY_ATTACHMENT = Attachment(
    media_type=None,
    mime_type=None,
    file_id=None,
    file_name=None,
    content_override=None,
)


def _simple(media_type: str):
    """
    Строит извлекатель для вложений, у которых разбирать нечего: document, animation, video
    и audio сами несут mime_type, file_id и file_name - отличается только подпись типа,
    которая идет в media_type и в служебную пометку вложения (см. notes.MEDIA_LABELS).

    :param media_type: значение, которое ляжет в Attachment.media_type
    :type media_type: str
    :return: функция (объект вложения) -> Attachment
    """

    def build(raw) -> Attachment:
        return Attachment(media_type, raw.mime_type, raw.file_id, raw.file_name, None)

    return build


def _photo(photo) -> Attachment:
    """
    Строит вложение для фото.

    :param photo: message.photo - один и тот же снимок в нескольких размерах, от
        маленького к большому
    :return: разобранное вложение
    :rtype: Attachment
    """
    # Модели нужен самый крупный вариант - он последний в списке.
    largest = photo[-1]
    # У PhotoSize в принципе нет поля mime_type - Telegram всегда отдает фото как JPEG и
    # читать тут просто нечего, поэтому mime зашит константой.
    return Attachment("photo", "image/jpeg", largest.file_id, None, None)


def _sticker(sticker) -> Attachment:
    """
    Строит вложение для стикера.

    Вид стикера (обычный/анимированный/видео) и его mime зависят от is_animated/is_video,
    а не только от самого факта "это стикер" - разбор вынесен в describe_sticker, чтобы не
    дублировать его тут и в пометке для модели.

    :param sticker: message.sticker
    :return: разобранное вложение
    :rtype: Attachment
    """
    media_type, file_id, mime_type = notes.describe_sticker(sticker)
    return Attachment(media_type, mime_type, file_id, None, None)


def _voice(voice) -> Attachment:
    """
    Строит вложение для голосового сообщения.

    :param voice: message.voice
    :return: разобранное вложение
    :rtype: Attachment
    """
    # У Voice mime_type - поле необязательное и приходит не всегда, а голосовые в
    # Telegram всегда OGG/Opus, так что mime здесь так же зашит константой, как и у photo.
    return Attachment("voice", "audio/ogg", voice.file_id, None, None)


def _video_note(video_note) -> Attachment:
    """
    Строит вложение для видеосообщения-кружка.

    :param video_note: message.video_note
    :return: разобранное вложение
    :rtype: Attachment
    """
    # У VideoNote поля mime_type нет вовсе - Telegram сам перегоняет кружки в mp4 и об
    # этом никак не сообщает.
    return Attachment("video_note", "video/mp4", video_note.file_id, None, None)


# Таблица (как достать объект вложения из сообщения, как превратить его в Attachment).
# Порядок пар дословно повторяет прежний if/elif и он значим: sticker обязан проверяться
# раньше animation и video, потому что у видеостикера заполнены сразу оба поля - и
# message.sticker, и video-подобное. Поменяй пары местами - и видеостикер молча
# превратится в обычное видео или гифку, хотя в чате остался тем же стикером.
_EXTRACTORS = (
    (lambda message: message.photo, _photo),
    (lambda message: message.document, _simple("document")),
    (lambda message: message.sticker, _sticker),
    (lambda message: message.animation, _simple("animation")),
    (lambda message: message.video, _simple("video")),
    (lambda message: message.audio, _simple("audio")),
    (lambda message: message.voice, _voice),
    (lambda message: message.video_note, _video_note),
)

# Вложения, у которых текст реплики в базе подменяется описанием того, кто его записал, а
# не расшифровкой - Telegram голосовые и кружки в текст не переводит, и оставлять на их
# месте пустую строку или сырой file_id смысла нет.
_CONTENT_OVERRIDE_LABELS = {
    "voice": "Голосовое сообщение",
    "video_note": "Видео сообщение",
}


def extract_attachment(message: Message) -> Attachment:
    """
    Разбирает вложение сообщения Telegram, если оно есть.

    :param message: сообщение Telegram
    :type message: Message
    :return: разобранное вложение или EMPTY_ATTACHMENT, если в сообщении вложения нет
    :rtype: Attachment
    """
    for get_raw, build in _EXTRACTORS:
        raw = get_raw(message)
        if raw:
            attachment = build(raw)
            label = _CONTENT_OVERRIDE_LABELS.get(attachment.media_type)
            if label:
                # Автор берется из самого сообщения, а не из объекта вложения: ни у
                # Voice, ни у VideoNote своего автора нет - есть только у message.
                attachment = replace(
                    attachment,
                    content_override=f"[{label} by {message.from_user.username}]",
                )
            return attachment
    return EMPTY_ATTACHMENT
