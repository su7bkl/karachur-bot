"""
Конвертация "неудобных" документов в PDF - единственный документный формат, который
Gemini понимает наверняка.

doc/docx/odt/rtf/xls/xlsx/ods/ppt/pptx/odp/epub модель не читает вовсе, а PDF - прекрасно,
поэтому такие файлы прогоняются через LibreOffice в headless-режиме перед отправкой модели.
Само решение "нужно ли вообще конвертировать этот файл" здесь не принимается - это дело
policy, а этот модуль только переводит формат, который ему уже подобрали.

Бот обслуживает несколько чатов параллельно, и это ставит LibreOffice в две классические
ловушки: общий профиль в ~/.config/libreoffice, за который дерутся параллельные процессы
(один из них либо падает, либо виснет насмерть), и отсутствие HOME под systemd, на котором
soffice спотыкается сразу же. Обе решаются одинаково - каждому вызову свой временный
каталог, который служит и профилем (-env:UserInstallation), и значением HOME, и убирается
по окончании вызова.

Как и с ffmpeg (см. соседний ffmpeg.py), код возврата процесса тут не показатель: soffice
умеет отрапортовать успех, не создав ни одного файла. Единственный надежный критерий -
появился ли на диске результат ненулевого размера, поэтому проверяется именно он.
"""

import logging
import os
import shutil
import signal
import subprocess
import tempfile

from karachur.media.ffmpeg import CONVERSION_TIMEOUT

logger = logging.getLogger(__name__)

SOFFICE = "soffice"


def run_soffice(source: str, outdir: str, profile: str) -> bool:
    """
    Зовет soffice. Отдельной функцией - чтобы тесты могли ее подменить.

    Возвращает лишь то, что процесс отработал сам по себе (запустился, не завис, не
    пожаловался кодом возврата) - а не то, что результат действительно появился на диске:
    soffice способен вернуть 0, ничего не создав, и эту проверку делает уже to_pdf.

    :param source: путь к исходному документу
    :type source: str
    :param outdir: каталог, куда soffice положит pdf
    :type outdir: str
    :param profile: отдельный временный каталог профиля для этого вызова
    :type profile: str
    :return: True, если процесс завершился без таймаута, ошибки ОС и ненулевого кода
    :rtype: bool
    """
    command = [
        SOFFICE, f"-env:UserInstallation=file://{profile}",
        "--headless", "--convert-to", "pdf", "--outdir", outdir, source,
    ]
    # HOME задаем явно и в тот же каталог, что и профиль: под systemd переменной может
    # не быть вовсе, и soffice на этом падает; заодно весь его собственный мусор уедет
    # в каталог, который мы все равно целиком удалим в finally у to_pdf.
    env = dict(os.environ, HOME=profile)
    try:
        # start_new_session=True заводит новую группу процессов: soffice плодит потомков,
        # и без отдельной группы таймаут убил бы только прямого ребенка, а зависшие
        # процессы остались бы жить дальше сами по себе. Popen открываем через "with" -
        # так его пайпы гарантированно закроются, даже если ниже что-то пойдет не так.
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, start_new_session=True,
        ) as proc:
            try:
                _, stderr = proc.communicate(timeout=CONVERSION_TIMEOUT)
            except subprocess.TimeoutExpired:
                logger.warning("soffice завис на %s, убиваю всю группу процессов.", source)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
                # Добираем communicate без таймаута: процесс уже убит, это лишь
                # освобождает пайпы и не даст зомби повиснуть.
                proc.communicate()
                return False
    except OSError as e:
        logger.warning("Не удалось запустить soffice для %s: %s", source, e)
        return False

    if proc.returncode != 0:
        error = stderr.decode("utf-8", "replace").strip().splitlines()
        logger.warning(
            "soffice не смог сконвертировать %s: %s",
            source,
            error[-1] if error else f"код {proc.returncode}",
        )
        return False
    return True


def to_pdf(path: str) -> str | None:
    """
    Конвертирует документ в PDF рядом с исходником через LibreOffice.

    Исходник не удаляется - об этом заботится вызывающий код, у которого есть
    транзакция на весь набор файлов сообщения. При любой неудаче тихо возвращает None,
    ничего не поднимая наружу: бот обязан продолжать работать и без LibreOffice в системе.

    :param path: путь к исходному документу
    :type path: str
    :return: путь к получившемуся pdf либо None, если конвертация не удалась
    :rtype: str | None
    """
    if shutil.which(SOFFICE) is None:
        logger.warning("soffice не найден, %s отправится модели как есть.", path)
        return None

    # Профиль отдельный на каждый вызов: два soffice, которые делят общий профиль в
    # ~/.config/libreoffice, дерутся за блокировку, и один из них либо падает, либо
    # виснет насмерть. Бот обслуживает несколько чатов одновременно, так что это не
    # теоретический случай, а рабочий режим.
    profile = tempfile.mkdtemp(prefix="karachur-soffice-profile-")
    # Конвертируем во временный каталог, а не сразу рядом с исходником: если там уже
    # лежит чужой старый pdf с тем же именем, soffice его не тронет, а мы бы молча
    # забрали этот чужой файл вместо настоящего результата.
    staging = tempfile.mkdtemp(prefix="karachur-soffice-out-")
    target = None
    try:
        if not run_soffice(path, staging, profile):
            return None

        # Ловушка с именем: soffice кладет результат как <имя без расширения>.pdf,
        # то есть по basename исходника, а не по полному пути к нему.
        name = os.path.splitext(os.path.basename(path))[0] + ".pdf"
        produced = os.path.join(staging, name)
        if not os.path.exists(produced) or os.path.getsize(produced) == 0:
            # Именно эта проверка, а не код возврата: soffice умеет отчитаться об
            # успехе, не создав ни одного файла.
            logger.warning("soffice отчитался об успехе, но %s не появился.", produced)
            return None

        target = os.path.splitext(path)[0] + ".pdf"
        try:
            # shutil.move, а не os.replace: staging лежит в /tmp и может оказаться на
            # другой файловой системе, чем исходник, а os.replace на таком падает с EXDEV.
            shutil.move(produced, target)
        except OSError as e:
            logger.warning("Не удалось перенести %s к исходнику: %s", produced, e)
            return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)

    logger.info("Документ сконвертирован в pdf: %s -> %s", path, target)
    return target
