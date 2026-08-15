"""
Тесты перекодирования медиа.

Правила подбора формата проверяются без ffmpeg, а сама перекодировка - настоящим
вызовом на файлах, которые ffmpeg тут же и создает. Если ffmpeg в системе нет, эти
тесты пропускаются: бот без него тоже работает, просто отдает файлы как есть.
"""

import asyncio
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import bot
import media

FFMPEG_MISSING = shutil.which("ffmpeg") is None
needs_ffmpeg = pytest.mark.skipif(FFMPEG_MISSING, reason="в системе нет ffmpeg")


@pytest.mark.parametrize(
    "mime, expected",
    [
        ("video/webm", "video/mp4"),
        ("video/mp4", "video/mp4"),
        ("video/quicktime", "video/mp4"),
        ("image/gif", "video/mp4"),
        ("audio/ogg", "audio/aac"),
        ("audio/mpeg", "audio/aac"),
        ("image/webp", "image/png"),
        ("image/heic", "image/png"),
        ("image/jpeg", None),
        ("image/png", None),
        ("application/pdf", None),
        ("text/plain", None),
        (None, None),
    ],
)
def test_conversion_rules(mime, expected):
    """Каждому типу подбирается свой формат, а лишнее не трогается."""
    rule = media.conversion_for(mime)
    assert (rule[1] if rule else None) == expected


def test_mime_with_parameters_is_understood():
    """Mime с довеском разбирается наравне с чистым."""
    rule = media.conversion_for("VIDEO/WEBM; codecs=vp9")
    assert rule is not None and rule[1] == "video/mp4"


def test_missing_file_is_left_alone(tmp_path):
    """Несуществующий файл не превращается ни во что."""
    absent = str(tmp_path / "нет.webm")
    assert media.normalize(absent, "video/webm") == (absent, "video/webm")


def test_untouched_types_skip_ffmpeg(tmp_path, monkeypatch):
    """Для jpeg ffmpeg вообще не зовется."""
    called = []
    monkeypatch.setattr(media, "run_ffmpeg", lambda *a: called.append(a) or True)
    photo = tmp_path / "фото.jpg"
    photo.write_bytes(b"not a real jpeg")

    assert media.normalize(str(photo), "image/jpeg") == (str(photo), "image/jpeg")
    assert not called


def test_failed_conversion_keeps_the_original(tmp_path, monkeypatch):
    """Если ffmpeg не справился, остается исходный файл и исходный mime."""
    monkeypatch.setattr(media, "run_ffmpeg", lambda *a: False)
    source = tmp_path / "стикер.webm"
    source.write_bytes(b"broken webm")

    assert media.normalize(str(source), "video/webm") == (str(source), "video/webm")
    assert source.exists()
    assert not list(tmp_path.glob("*.converting.*"))


def test_missing_ffmpeg_keeps_the_original(tmp_path, monkeypatch):
    """Без ffmpeg бот работает по-прежнему, просто без перекодирования."""
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    source = tmp_path / "стикер.webm"
    source.write_bytes(b"webm")

    assert media.normalize(str(source), "video/webm") == (str(source), "video/webm")


def make_video(path, args):
    """Создает тестовый файл через сам ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", *args, str(path)],
        check=True,
        capture_output=True,
    )


@needs_ffmpeg
def test_video_becomes_mp4(tmp_path):
    """Видео перекодируется в mp4, исходник убирается."""
    source = tmp_path / "стикер.webm"
    make_video(source, ["-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10"])

    path, mime = media.normalize(str(source), "video/webm")

    assert mime == "video/mp4"
    assert path.endswith(".mp4")
    assert os.path.exists(path)
    assert not source.exists()


@needs_ffmpeg
def test_odd_sides_do_not_break_encoding(tmp_path):
    """Нечетная сторона кадра не валит H.264 - как у стикера 489x512."""
    source = tmp_path / "стикер.webm"
    make_video(source, ["-f", "lavfi", "-i", "testsrc=duration=1:size=489x512:rate=10"])

    path, mime = media.normalize(str(source), "video/webm")

    assert mime == "video/mp4"
    assert os.path.getsize(path) > 0


@needs_ffmpeg
def test_broken_duration_is_repaired(tmp_path):
    """
    Контейнер с рассогласованной длительностью пересобирается в согласованный.

    Ровно на таком файле Gemini отвечала неустранимой ошибкой 400: в контейнере стояло
    33 мс при шестисекундной дорожке.
    """
    source = tmp_path / "битый.webm"
    make_video(
        source,
        ["-f", "lavfi", "-i", "testsrc=duration=6:size=64x64:rate=30", "-metadata",
         "duration=0.033"],
    )

    path, _ = media.normalize(str(source), "video/webm")
    probed = subprocess.run(
        ["ffprobe", "-loglevel", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        check=True, capture_output=True, text=True,
    )

    assert abs(float(probed.stdout.strip()) - 6.0) < 0.5


@needs_ffmpeg
def test_audio_becomes_aac(tmp_path):
    """Голосовое переводится в aac."""
    source = tmp_path / "голос.ogg"
    make_video(source, ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"])

    path, mime = media.normalize(str(source), "audio/ogg")

    assert mime == "audio/aac"
    assert path.endswith(".aac")
    assert os.path.getsize(path) > 0


@needs_ffmpeg
def test_webp_becomes_png(tmp_path):
    """Статичный стикер переводится в png."""
    source = tmp_path / "стикер.webp"
    make_video(source, ["-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=1",
                        "-frames:v", "1"])

    path, mime = media.normalize(str(source), "image/webp")

    assert mime == "image/png"
    assert path.endswith(".png")
    assert os.path.getsize(path) > 0


@needs_ffmpeg
def test_mp4_is_rebuilt_in_place(tmp_path):
    """Даже mp4 пересобирается - именно так чинятся битые заголовки."""
    source = tmp_path / "видео.mp4"
    make_video(source, ["-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10"])

    path, mime = media.normalize(str(source), "video/mp4")

    assert (path, mime) == (str(source), "video/mp4")
    assert os.path.getsize(path) > 0
    assert not list(tmp_path.glob("*.converting.*"))


def test_stored_media_path_wins(db, tmp_path, monkeypatch):
    """Для перекодированного файла берется путь из базы, а не вычисленный по mime."""
    converted = tmp_path / "готовое.mp4"
    converted.write_bytes(b"video")
    db.execute(
        """
        INSERT INTO messages (message_id, chat_id, username, content, mime_type,
                              file_id, media_path, timestamp, is_bot, summarized)
        VALUES (1, -100, 'tester', 'вот видео', 'video/mp4', 'FILE', ?,
                '2026-08-15T10:00:00', 0, 0)
        """,
        (str(converted),),
    )
    db.commit()

    used = []
    monkeypatch.setattr(bot, "check_file_validity", lambda c, k, p: used.append(p))
    monkeypatch.setattr(bot, "upload_file", lambda c, k, p: used.append(p))

    _, messages = bot.get_context(db, -100)
    bot.build_message_parts(None, "ключ", messages[0])

    assert used == [str(converted), str(converted)]


def test_normalize_media_records_the_result(db, tmp_path, monkeypatch):
    """Путь и mime после перекодирования попадают в базу."""
    source = tmp_path / "исходник.webm"
    source.write_bytes(b"webm")
    db.execute(
        """
        INSERT INTO messages (message_id, chat_id, username, content, mime_type,
                              file_id, timestamp, is_bot, summarized)
        VALUES (7, -100, 'tester', 'стикер', 'video/webm', 'FILE',
                '2026-08-15T10:00:00', 0, 0)
        """
    )
    db.commit()

    monkeypatch.setattr(media, "normalize", lambda p, m: (str(tmp_path / "и.mp4"), "video/mp4"))
    message = SimpleNamespace(chat_id=-100, message_id=7)

    asyncio.run(bot.normalize_media(db, message, str(source), "video/webm"))

    stored = db.execute(
        "SELECT media_path, mime_type FROM messages WHERE message_id = 7"
    ).fetchone()
    assert stored == (str(tmp_path / "и.mp4"), "video/mp4")
