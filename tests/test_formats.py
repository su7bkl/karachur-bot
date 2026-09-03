"""
Тесты работы с форматами, которые Gemini не понимает.

Пять слоев, снизу вверх. Первый - политика: decide() раскладывает пары (mime, имя файла)
по пяти действиям, ничего не конвертируя и почти не трогая сам файл. Второй - конвертация
документов в PDF через LibreOffice: живая проверка идет настоящим вызовом soffice на .rtf,
который читается LibreOffice и собирается голым текстом, а остальные грабли (профиль,
HOME, честная проверка результата, убийство зависшего процесса) проверяются подменами,
чтобы не зависеть от машины. Третий - media.normalize целиком: именно ее зовет обработчик
сообщений, и именно ее контракт (путь, mime) не должен меняться.

Четвертый и пятый - два места, где решение политики наконец исполняется. Барьер перед
выгрузкой в Files API (karachur.gemini.contents): формат вне белого списка не уезжает к
модели, а превращается в текстовую пометку. Ранний отказ до скачивания
(karachur.tg.handlers): отвергнутое политикой вложение не качается вовсе. Оба нужны
против одного и того же: Gemini отвечает на нечитаемый файл неустранимой ошибкой 400, и
файл, оставшийся в истории, глушит чат на всех последующих запросах.

Отдельное внимание двум ловушкам: точное совпадение mime должно перехватываться раньше
группового префикса, а определение текста по содержимому не должно спотыкаться о
многобайтовый символ, разрезанный границей чтения в 8 КБ. Если soffice в системе нет,
живые тесты пропускаются: бот без LibreOffice тоже работает, просто без конвертации.
"""

import asyncio
import os
import shutil
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from karachur import media
from karachur.gemini import contents, files
from karachur.media import documents, policy
from karachur.media.ffmpeg import CONVERSION_TIMEOUT
from karachur.media.policy import Action
from karachur.tg import handlers

SOFFICE_MISSING = shutil.which("soffice") is None
needs_soffice = pytest.mark.skipif(SOFFICE_MISSING, reason="в системе нет LibreOffice")

RTF_TEXT = r"{\rtf1\ansi\deff0 Живой тест конвертации документа.}"


# --- ПОЛИТИКА ФОРМАТОВ ---

@pytest.mark.parametrize(
    "mime, expected_action, expected_mime",
    [
        ("image/jpeg", Action.KEEP, "image/jpeg"),
        ("video/mp4", Action.ENCODE, "video/mp4"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            Action.PDF,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("text/x-python", Action.RETAG, "text/plain"),
        ("application/zip", Action.SKIP, None),
    ],
)
def test_five_actions_cover_every_outcome(tmp_path, mime, expected_action, expected_mime):
    """На каждое из пяти действий находится свой mime."""
    path = str(tmp_path / "файл")
    assert policy.decide(mime, path) == (expected_action, expected_mime)


def test_exact_mime_wins_over_group_prefix(tmp_path):
    """image/webp входит в белый список, но точное правило важнее и уводит в ENCODE."""
    path = str(tmp_path / "стикер.webp")
    assert policy.decide("image/webp", path) == (Action.ENCODE, "image/webp")


def test_gif_goes_to_mp4_encode_branch(tmp_path):
    """image/gif в белом списке нет специально: гифка обязана уйти в mp4-ветку ffmpeg."""
    path = str(tmp_path / "гифка.gif")
    assert policy.decide("image/gif", path) == (Action.ENCODE, "image/gif")


@pytest.mark.parametrize("mime", ["video/mp4", "audio/ogg"])
def test_whitelisted_media_is_still_encoded(tmp_path, mime):
    """Видео и аудио пересобираются ffmpeg-ом, даже если mime уже в белом списке."""
    path = str(tmp_path / "медиа")
    assert policy.decide(mime, path) == (Action.ENCODE, mime)


@pytest.mark.parametrize(
    "name, expected_action, expected_mime",
    [
        ("документ.docx", Action.PDF,
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("гифка.gif", Action.ENCODE, "image/gif"),
        ("скрипт.py", Action.RETAG, "text/plain"),
        ("архив.zip", Action.SKIP, None),
        ("фото.png", Action.KEEP, "image/png"),
    ],
)
def test_octet_stream_is_resolved_by_extension(tmp_path, name, expected_action, expected_mime):
    """Телеграм присылает application/octet-stream для незнакомых расширений - разбираем сами."""
    path = tmp_path / name
    path.write_bytes(b"content")
    assert policy.decide("application/octet-stream", str(path)) == (
        expected_action, expected_mime
    )


def test_text_recognized_by_content_when_name_has_no_extension(tmp_path):
    """Без расширения и без внятного mime текст всё равно опознаётся - по содержимому."""
    path = tmp_path / "пересланное_без_имени"
    path.write_text("обычный текст без всякого расширения", encoding="utf-8")
    assert policy.decide("application/octet-stream", str(path)) == (Action.RETAG, "text/plain")


def test_binary_garbage_without_extension_is_skipped(tmp_path):
    """Двоичный мусор без расширения и без mime модели не отдаётся."""
    path = tmp_path / "неизвестное_нечто"
    path.write_bytes(b"\x00\x01\x02\xff\xfe\x00binary")
    assert policy.decide(None, str(path)) == (Action.SKIP, None)


def test_mime_with_parameters_is_normalized(tmp_path):
    """Довесок вида "; charset=utf-8" не мешает разобрать mime."""
    path = str(tmp_path / "заметка.txt")
    assert policy.decide("TEXT/PLAIN; charset=utf-8", path) == (Action.KEEP, "text/plain")


def test_none_mime_falls_back_to_extension(tmp_path):
    """Если Телеграм вообще не прислал mime, решение принимается по расширению файла."""
    path = str(tmp_path / "фото.png")
    assert policy.decide(None, path) == (Action.KEEP, "image/png")


def test_looks_like_text_true_for_plain_utf8(tmp_path):
    """Обычный UTF-8 текст опознаётся функцией напрямую."""
    path = tmp_path / "текст.dat"
    path.write_text("привет, это просто текст", encoding="utf-8")
    assert policy.looks_like_text(str(path)) is True


def test_looks_like_text_rejects_null_bytes(tmp_path):
    """Нулевой байт в содержимом - надёжный признак двоичного формата, не текста."""
    path = tmp_path / "мусор.dat"
    path.write_bytes(b"kind of text\x00but not really")
    assert policy.looks_like_text(str(path)) is False


def test_looks_like_text_survives_multibyte_char_split_across_8kb_boundary(tmp_path):
    """
    Символ, разрезанный ровно на границе чтения в 8 КБ, не должен превращать текст в мусор.
    """
    # 8191 однобайтовый символ плюс первый байт двухбайтовой "б" - ровно 8192 байта в
    # прочитанном куске, второй байт символа остаётся уже за границей чтения.
    head = b"a" * (policy.TEXT_SNIFF_SIZE - 1)
    split_char = "б".encode("utf-8")
    path = tmp_path / "разрезанный.dat"
    path.write_bytes(head + split_char + "остаток текста".encode("utf-8"))

    assert policy.looks_like_text(str(path)) is True


# --- КОНВЕРТАЦИЯ ДОКУМЕНТОВ В PDF ---


@needs_soffice
def test_live_conversion_produces_a_real_pdf(tmp_path):
    """Настоящий soffice превращает .rtf в файл, начинающийся с %PDF."""
    source = tmp_path / "документ.rtf"
    source.write_text(RTF_TEXT, encoding="utf-8")

    result = documents.to_pdf(str(source))

    assert result == str(tmp_path / "документ.pdf")
    assert os.path.getsize(result) > 0
    with open(result, "rb") as f:
        assert f.read(4) == b"%PDF"
    assert source.exists()


def test_missing_soffice_keeps_the_original(tmp_path, monkeypatch):
    """Без soffice в системе to_pdf возвращает None, а исходник остается на месте."""
    monkeypatch.setattr(documents.shutil, "which", lambda name: None)
    source = tmp_path / "документ.docx"
    source.write_bytes(b"not a real docx")

    assert documents.to_pdf(str(source)) is None
    assert source.exists()


def test_zero_exit_without_a_file_is_not_success(tmp_path, monkeypatch):
    """Соффис вернул успех, но файла нет - to_pdf не должен в это поверить."""
    monkeypatch.setattr(documents.shutil, "which", lambda name: "/usr/bin/soffice")
    monkeypatch.setattr(documents, "run_soffice", lambda source, outdir, profile: True)
    source = tmp_path / "документ.odt"
    source.write_bytes(b"not a real odt")

    assert documents.to_pdf(str(source)) is None
    assert source.exists()
    assert not (tmp_path / "документ.pdf").exists()


def test_successful_conversion_keeps_the_source_and_moves_the_result(tmp_path, monkeypatch):
    """После удачной конвертации pdf появляется рядом, а исходник остается нетронутым."""
    monkeypatch.setattr(documents.shutil, "which", lambda name: "/usr/bin/soffice")

    def fake_run_soffice(source, outdir, _profile):
        # Соффис кладет результат как <имя без расширения>.pdf прямо в outdir.
        name = os.path.splitext(os.path.basename(source))[0] + ".pdf"
        with open(os.path.join(outdir, name), "wb") as f:
            f.write(b"%PDF-1.7 fake")
        return True

    monkeypatch.setattr(documents, "run_soffice", fake_run_soffice)
    source = tmp_path / "документ.docx"
    source.write_bytes(b"original content")

    result = documents.to_pdf(str(source))

    assert result == str(tmp_path / "документ.pdf")
    assert os.path.exists(result)
    assert source.exists()
    assert source.read_bytes() == b"original content"


def test_each_call_gets_its_own_profile_and_it_is_removed(tmp_path, monkeypatch):
    """У каждого вызова свой каталог профиля, и он исчезает после работы."""
    monkeypatch.setattr(documents.shutil, "which", lambda name: "/usr/bin/soffice")
    seen_profiles = []

    def fake_run_soffice(_source, _outdir, profile):
        # В момент вызова каталог профиля должен уже существовать.
        assert os.path.isdir(profile)
        seen_profiles.append(profile)
        return True

    monkeypatch.setattr(documents, "run_soffice", fake_run_soffice)
    first = tmp_path / "первый.docx"
    second = tmp_path / "второй.docx"
    first.write_bytes(b"1")
    second.write_bytes(b"2")

    documents.to_pdf(str(first))
    documents.to_pdf(str(second))

    assert len(seen_profiles) == 2
    assert seen_profiles[0] != seen_profiles[1]
    for profile in seen_profiles:
        assert not os.path.exists(profile)


def test_command_line_and_environment_carry_the_profile(tmp_path, monkeypatch):
    """В команде реально есть -env:UserInstallation=file://, а в окружении - HOME."""
    captured = {}

    class FakeProcess:
        """Заглушка процесса: soffice не запускается, аргументы только собираются."""

        pid = 12345
        returncode = 0

        def __enter__(self):
            """Ведет себя как настоящий Popen - поддерживает менеджер контекста."""
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            """Закрывать нечего - пайпов у заглушки нет."""
            return False

        def communicate(self, timeout=None):
            """Сразу отдает пустой вывод, как будто soffice отработал мгновенно."""
            captured["timeout"] = timeout
            return b"", b""

    def fake_popen(command, env=None, start_new_session=None, **_kwargs):
        captured["command"] = command
        captured["env"] = env
        captured["new_session"] = start_new_session
        return FakeProcess()

    monkeypatch.setattr(documents.subprocess, "Popen", fake_popen)
    profile = tmp_path / "профиль"
    profile.mkdir()
    source = tmp_path / "документ.docx"
    source.write_bytes(b"1")
    outdir = tmp_path / "out"
    outdir.mkdir()

    ok = documents.run_soffice(str(source), str(outdir), str(profile))

    assert ok is True
    install_args = [a for a in captured["command"] if a.startswith("-env:UserInstallation=")]
    assert install_args == [f"-env:UserInstallation=file://{profile}"]
    assert captured["env"]["HOME"] == str(profile)
    # Лимит тот же, что у ffmpeg, и процесс заводится своей группой - иначе таймаут
    # прибил бы только прямого потомка, а зависшие процессы остались бы жить.
    assert captured["timeout"] == CONVERSION_TIMEOUT
    assert captured["new_session"] is True


def test_hung_soffice_is_killed_as_a_whole_process_group(tmp_path, monkeypatch):
    """Зависший soffice убивается вместе со всеми потомками, а не только сам процесс."""
    killed = {}

    class HangingProcess:
        """Заглушка процесса, который никогда не завершается сам."""

        pid = 54321

        def __enter__(self):
            """Ведет себя как настоящий Popen - поддерживает менеджер контекста."""
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            """Закрывать нечего - пайпов у заглушки нет."""
            return False

        def communicate(self, timeout=None):
            """Первый вызов имитирует таймаут, второй - уборку после убийства."""
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="soffice", timeout=timeout)
            return b"", b""

    monkeypatch.setattr(documents.subprocess, "Popen", lambda *a, **k: HangingProcess())
    monkeypatch.setattr(documents.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(documents.os, "killpg", lambda pgid, sig: killed.setdefault("pgid", pgid))

    ok = documents.run_soffice(str(tmp_path / "документ.docx"), str(tmp_path), str(tmp_path))

    assert ok is False
    assert killed["pgid"] == 54321


# --- ВХОД, КОТОРЫМ ПОЛЬЗУЕТСЯ БОТ ---

# Минимальный, но настоящий docx: LibreOffice читает именно такой пакет, а собрать его
# можно без сторонних библиотек. Ровно в этом виде документы и приходят из Телеграма.
DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels"
 ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-\
officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Target="word/document.xml"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>
</Relationships>"""
DOCX_BODY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>Документ, который Gemini понимает только в виде PDF.</w:t></w:r></w:p>
</w:body></w:document>"""
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def make_docx(path):
    """Собирает настоящий docx-пакет из трех обязательных частей."""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", DOCX_CONTENT_TYPES)
        package.writestr("_rels/.rels", DOCX_RELS)
        package.writestr("word/document.xml", DOCX_BODY)


@needs_soffice
def test_document_comes_back_from_normalize_as_pdf(tmp_path):
    """
    Docx уходит в normalize и возвращается оттуда парой (файл.pdf, application/pdf).

    Это и есть весь смысл затеи: обработчик зовет только normalize, и конвертация документов
    заводится без единой правки за пределами пакета.
    """
    source = tmp_path / "отчёт.docx"
    make_docx(source)

    path, mime = media.normalize(str(source), DOCX_MIME)

    assert mime == "application/pdf"
    assert path == str(tmp_path / "отчёт.pdf")
    with open(path, "rb") as result:
        assert result.read(4) == b"%PDF"
    # Исходник больше не нужен: в контекст пойдет pdf, и путь к нему уедет в базу.
    assert not source.exists()


def test_missing_libreoffice_keeps_the_document(tmp_path, monkeypatch):
    """Без LibreOffice бот работает по-прежнему, документ уходит модели как есть."""
    monkeypatch.setattr(documents.shutil, "which", lambda name: None)
    source = tmp_path / "отчёт.docx"
    source.write_bytes("не настоящий docx".encode("utf-8"))

    assert media.normalize(str(source), DOCX_MIME) == (str(source), DOCX_MIME)
    assert source.exists()


def test_archive_is_not_offered_to_the_model(tmp_path):
    """У архива mime обнуляется - обработчик грузит файл только при непустом mime."""
    source = tmp_path / "архив.zip"
    source.write_bytes(b"PK\x03\x04\x00\x00")

    assert media.normalize(str(source), "application/zip") == (str(source), None)
    # Файл не трогаем: он еще может понадобиться, модели просто не показываем.
    assert source.exists()


def test_source_code_reaches_the_model_as_plain_text(tmp_path):
    """Исходник под octet-stream меняет только mime, сам файл остается нетронутым."""
    source = tmp_path / "скрипт.py"
    source.write_text("print('привет')\n", encoding="utf-8")

    path, mime = media.normalize(str(source), "application/octet-stream")

    assert (path, mime) == (str(source), "text/plain")
    assert source.read_text(encoding="utf-8") == "print('привет')\n"


def test_whitelisted_pdf_is_left_alone(tmp_path, monkeypatch):
    """Готовый pdf никуда не конвертируется - ни в ffmpeg, ни в LibreOffice."""
    called = []
    monkeypatch.setattr(documents, "to_pdf", called.append)
    source = tmp_path / "инструкция.pdf"
    source.write_bytes(b"%PDF-1.7 ")

    assert media.normalize(str(source), "application/pdf") == (
        str(source), "application/pdf"
    )
    assert not called


def test_bot_made_bin_extension_falls_through_to_content(tmp_path):
    """
    Файл без mime бот называет <file_id>.bin - решать по такому имени нечего.

    Расширение придумано самим ботом, а не отправителем, поэтому единственный источник
    правды тут - содержимое: текст должен дойти до модели, а не пропасть как "двоичное".
    """
    text = tmp_path / "FILEID.bin"
    text.write_text("заметки, присланные без всякого mime", encoding="utf-8")
    binary = tmp_path / "FILEID2.bin"
    binary.write_bytes(b"\x7fELF\x02\x01\x00\x00\x00")

    assert policy.decide(None, str(text)) == (Action.RETAG, "text/plain")
    assert policy.decide(None, str(binary)) == (Action.SKIP, None)


# --- БАРЬЕР ПЕРЕД ВЫГРУЗКОЙ В FILES API ---

# Сообщение с вложением в том виде, в каком его отдает база: сборке запроса нужны от
# строки только автор, текст и три колонки про файл.
def media_message(path, mime):
    """Собирает строку messages с вложением - ровно то, что читает build_message_parts."""
    return {
        "username": "tester",
        "content": "лови файл",
        "file_id": "FILEID",
        "mime_type": mime,
        "media_path": str(path),
    }


def watch_uploads(monkeypatch):
    """
    Подменяет выгрузку в Files API и возвращает список путей, которые до нее дошли.

    Подменяются атрибуты самого karachur.gemini.files: сборка запроса зовет выгрузку
    через модуль, а не по импортированному имени, - иначе подмена бы ее не достала.
    """
    seen = []
    monkeypatch.setattr(files, "check_file_validity", lambda c, k, p: seen.append(p))
    monkeypatch.setattr(files, "upload_file", lambda c, k, p: seen.append(p))
    return seen


def test_unsupported_format_never_reaches_files_api(tmp_path, monkeypatch):
    """
    Файл с mime вне белого списка не выгружается, а превращается в текстовую пометку.

    Так в контекст попадает архив, доживший в базе до сборки запроса: раньше он уезжал
    в Files API и возвращался неустранимой ошибкой 400, глушившей чат целиком.
    """
    source = tmp_path / "архив.zip"
    source.write_bytes(b"PK\x03\x04\x00\x00")
    uploaded = watch_uploads(monkeypatch)

    parts = contents.build_message_parts(
        None, "ключ", media_message(source, "application/zip")
    )

    assert not uploaded
    assert parts[-1].text == "[Файл формата application/zip модель не читает - пропущено]"


def test_failed_pdf_conversion_also_stops_at_the_barrier(tmp_path, monkeypatch):
    """
    Docx, который не удалось перевести в PDF, до выгрузки тоже не доходит.

    Ради этого случая барьер и дублирует политику: политика отправила документ в PDF, но
    без LibreOffice решение не исполнилось, и docx остался docx.
    """
    monkeypatch.setattr(documents.shutil, "which", lambda name: None)
    source = tmp_path / "отчёт.docx"
    make_docx(source)

    path, mime = media.normalize(str(source), DOCX_MIME)
    # Конвертация не удалась - файл и mime остались прежними.
    assert (path, mime) == (str(source), DOCX_MIME)

    uploaded = watch_uploads(monkeypatch)
    parts = contents.build_message_parts(None, "ключ", media_message(path, mime))

    assert not uploaded
    assert parts[-1].text == f"[Файл формата {DOCX_MIME} модель не читает - пропущено]"


def test_supported_format_still_goes_up(tmp_path, monkeypatch):
    """Барьер выборочный: готовый pdf по-прежнему уезжает в Files API."""
    source = tmp_path / "инструкция.pdf"
    source.write_bytes(b"%PDF-1.7 ")
    uploaded = watch_uploads(monkeypatch)

    contents.build_message_parts(
        None, "ключ", media_message(source, "application/pdf")
    )

    assert uploaded == [str(source), str(source)]


# --- РАННИЙ ОТКАЗ ДО СКАЧИВАНИЯ ---


def run_handle_message(cfg, db, monkeypatch, mime, file_name):
    """
    Гоняет handle_message на сообщении с вложением и возвращает, что скачалось.

    Разбор объекта Telegram здесь не проверяется, поэтому save_message_to_db подменяется
    и сразу отдает описание вложения. Триггера в тексте нет - обработчик доходит до
    работы с файлом и возвращается, не трогая ни замок, ни модель.

    :return: (пути скачанного, пути перекодированного)
    :rtype: tuple[list, list]
    """
    downloaded = []
    normalized = []

    async def fake_download(_application, _file_id, file_path):
        """Запоминает попытку скачивания вместо похода в Telegram."""
        downloaded.append(file_path)

    async def fake_normalize(_conn, _message, file_path, _mime):
        """Запоминает попытку перекодирования."""
        normalized.append(file_path)

    monkeypatch.setattr(
        handlers.messages,
        "save_message_to_db",
        lambda conn, message, is_bot: ("FILEID", mime, file_name),
    )
    monkeypatch.setattr(handlers.paths, "download_media_file", fake_download)
    monkeypatch.setattr(handlers, "normalize_media", fake_normalize)

    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        chat_id=-100,
        message_id=1,
        text="файл без обращения к боту",
        caption=None,
        voice=None,
    )
    update = SimpleNamespace(effective_message=message)
    context = SimpleNamespace(bot_data={"cfg": cfg, "db_conn": db}, application=None)
    asyncio.run(handlers.handle_message(update, context))
    return downloaded, normalized


def test_archive_is_not_even_downloaded(cfg, db, monkeypatch):
    """Архив не качается вовсе: до модели он все равно не доедет."""
    downloaded, normalized = run_handle_message(
        cfg, db, monkeypatch, "application/zip", "архив.zip"
    )

    assert not downloaded
    assert not normalized


def test_unknown_mime_with_binary_extension_is_refused_by_name(cfg, db, monkeypatch):
    """
    Телеграм прислал octet-stream - решение принимается по расширению имени.

    Содержимого до скачивания нет, и единственное, что отличает установщик от текста, -
    его имя.
    """
    downloaded, _ = run_handle_message(
        cfg, db, monkeypatch, "application/octet-stream", "установщик.exe"
    )

    assert not downloaded


@pytest.mark.parametrize(
    "mime, file_name",
    [
        ("application/pdf", "инструкция.pdf"),
        # Ни mime, ни имя ничего не говорят: такой файл надо скачать и разобрать по
        # содержимому - иначе присланный без mime текст молча пропадет из контекста.
        ("application/octet-stream", None),
    ],
)
def test_files_that_may_still_be_useful_are_downloaded(
    cfg, db, monkeypatch, mime, file_name
):
    """Отказ выборочный: все, что политика не отвергла заранее, качается как раньше."""
    downloaded, normalized = run_handle_message(cfg, db, monkeypatch, mime, file_name)

    assert len(downloaded) == 1
    assert normalized == downloaded
