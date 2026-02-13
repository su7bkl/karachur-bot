"""
Модуль для разделения больших HTML-сообщений для Telegram.

Предоставляет функцию split_html_message(), которая разбивает HTML-строку на фрагменты,
соблюдая лимиты Telegram и целостность тегов.

Особенности:
    - Поддерживает только теги, разрешенные в Telegram Bot API:
      b, i, u, s, a, code, pre, tg-spoiler.
    - Автоматически нормализует HTML-теги к Telegram-совместимым:
      strong→b, em→i, ins→u, del/strike→s.
    - "Умное" разделение текста: старается не разрывать слова, делит по переносам строк или
    - пробелам.
    - Сохраняет атрибуты тегов (href в <a>, class в <code>) при переносе на следующую часть.
    - Игнорирует регистр тегов.

Пример использования:
    >>> html = "<b>Жирный</b> и <pre>код</pre>"
    >>> parts = split_html_message(html, max_chars=200)
"""

import re

# Теги, поддерживаемые Telegram Bot API
ALLOWED_TAGS = {
    "b",
    "i",
    "u",
    "s",
    "a",
    "code",
    "pre",
    "tg-spoiler",
}

# Маппинг HTML-тегов на Telegram-совместимые теги
# Эти теги будут автоматически преобразованы в процессе обработки
TAG_NORMALIZATION = {
    "strong": "b",
    "em": "i",
    "ins": "u",
    "del": "s",
    "strike": "s",
}

# Теги, которые не требуют закрытия или работают как разрывы
VOID_TAGS = {"br"}


def get_closing_str(stack):
    """Генерирует строку закрывающих тегов для текущего стека."""
    return "".join(f"</{tag_name}>" for tag_name, _ in reversed(stack))


def get_opening_str(stack):
    """Генерирует строку открывающих тегов для начала следующего чанка."""
    return "".join(full_tag for _, full_tag in stack)


def normalize_tag(token):
    """
    Нормализует HTML-тег к Telegram-совместимому формату.

    Преобразует теги вроде <strong>, <em>, <ins>, <del>, <strike>
    в их Telegram-эквиваленты: <b>, <i>, <u>, <s>.

    Args:
        token: HTML-тег в виде строки (например, "<strong>", "</em>")

    Returns:
        Нормализованный тег (например, "<b>", "</i>")
    """
    clean = token.strip("<> ")
    is_closing = clean.startswith("/")

    if is_closing:
        clean = clean[1:]

    # Берем первое слово (имя тега)
    tag_name = clean.split()[0].lower()

    # Если тег нужно нормализовать
    normalized_name = TAG_NORMALIZATION.get(tag_name)
    if normalized_name:
        # Сохраняем остальные атрибуты, если они есть
        rest_of_tag = clean[len(tag_name):]

        if is_closing:
            return f"</{normalized_name}>"

        return f"<{normalized_name}{rest_of_tag}>"

    return token


def extract_tag_info(token):
    """Возвращает (имя_тега, is_closing, is_void)."""
    clean = token.strip("<> ")
    is_closing = clean.startswith("/")
    if is_closing:
        clean = clean[1:]

    # Берем первое слово (имя тега)
    tag_name = clean.split()[0].lower()

    # Нормализуем имя тега, если нужно
    tag_name = TAG_NORMALIZATION.get(tag_name, tag_name)

    is_void = tag_name in VOID_TAGS or clean.endswith("/")

    return tag_name, is_closing, is_void


def normalize_html(html: str) -> str:
    """
    Нормализует HTML-теги к Telegram-совместимому формату.

    Args:
        html: Исходная HTML-строка

    Returns:
        HTML-строка с нормализованными тегами
    """
    # Разбиваем на теги и текст
    tokens = re.split(r"(<[^>]+>)", html)
    normalized_tokens = []

    for token in tokens:
        if token.startswith("<"):
            normalized_tokens.append(normalize_tag(token))
        else:
            normalized_tokens.append(token)

    return "".join(normalized_tokens)


def split_html_message(  # pylint: disable=too-many-locals,too-many-branches
    html: str, max_chars: int = 4096
) -> list[str]:
    """
    Разбивает HTML-сообщение на части не длиннее max_chars.

    Алгоритм:
    1. Токенизирует HTML.
    2. Накапливает токены в буфер.
    3. Если добавление токена (плюс необходимые закрывающие теги) превысит лимит:
       - Если это тег: закрываем текущий чанк, открываем новый.
       - Если это текст: ищем пробел/перенос для мягкого разрыва, переносим остаток.

    Параметры:
        html: Исходная строка
        max_chars: Максимальная длина одного сообщения (по умолчанию 4096 для Telegram)

    Возвращает:
        Список строк (чанков).
    """
    # Сначала нормализуем все теги к Telegram-совместимому формату
    html = normalize_html(html)

    if len(html) <= max_chars:
        return [html]

    # Разбиваем на теги и текст. Группировка () сохраняет разделители в списке.
    tokens = re.split(r"(<[^>]+>)", html)

    chunks = []
    current_chunk = ""
    # Стек хранит кортежи: (имя_тега, полный_текст_открывающего_тега)
    # Пример: ('a', '<a href="google.com">')
    tag_stack = []

    for token in tokens:  # pylint: disable=too-many-nested-blocks
        if not token:
            continue

        # --- Логика обработки ТЕГОВ ---
        if token.startswith("<"):
            # HTML уже нормализован на входе в функцию
            tag_name, is_closing, is_void = extract_tag_info(token)

            # Рассчитываем длину закрывающего хвоста
            closing_markup = get_closing_str(tag_stack)

            # Проверяем, влезает ли тег в текущий чанк
            if len(current_chunk) + len(token) + len(closing_markup) > max_chars:
                # Тег не влезает. Закрываем текущий чанк.
                chunks.append(current_chunk + closing_markup)
                # Начинаем новый.
                current_chunk = get_opening_str(tag_stack)
                # Если даже в новый пустой чанк тег не влезает (экстремально мало места)
                # то это патология, но мы добавим его, чтобы не потерять контент.

            current_chunk += token

            # Обновляем стек, если тег структурный и разрешенный
            if tag_name in ALLOWED_TAGS:
                if is_closing:
                    # Пытаемся закрыть последний соответствующий тег
                    # Ищем с конца, чтобы закрыть ближайший (хотя HTML должен быть валидным)
                    for tg in range(len(tag_stack) - 1, -1, -1):
                        if tag_stack[tg][0] == tag_name:
                            tag_stack.pop(tg)
                            break
                elif not is_void:
                    # Открывающий тег - добавляем в стек
                    tag_stack.append((tag_name, token))

            continue

        # --- Логика обработки ТЕКСТА ---
        text = token
        while text:
            closing_markup = get_closing_str(tag_stack)
            # Сколько места осталось для чистого текста
            available_space = max_chars - len(current_chunk) - len(closing_markup)

            if len(text) <= available_space:
                current_chunk += text
                text = ""  # Весь текст добавлен
            else:
                # Текст не влезает целиком. Нужно резать.
                # Ищем лучшее место для разреза в пределах available_space

                # Срез, который теоретически влезает
                candidate = text[:available_space]

                # Приоритет 1: Перенос строки (ищем последний \n)
                split_idx = candidate.rfind("\n")

                # Приоритет 2: Пробел (если нет переноса, ищем последний пробел)
                if split_idx == -1:
                    split_idx = candidate.rfind(" ")

                # Если вообще нет разделителей (очень длинное слово), режем жестко
                if split_idx == -1:
                    # Но если available_space слишком мал (меньше 10 символов),
                    # лучше сразу перенести всё слово на новый чанк, если чанк не пустой
                    if available_space < 10 and len(current_chunk) > len(
                        get_opening_str(tag_stack)
                    ):
                        split_idx = -1  # Сигнал "закрывай текущий чанк"
                    else:
                        split_idx = available_space
                else:
                    # Включаем разделитель в текущий кусок (или +1 если хотим выкинуть?)
                    # Обычно пробел оставляют в конце строки или убирают.
                    # text[:split_idx] берет до пробела.
                    # Чтобы пробел остался на этой строке: split_idx + 1
                    split_idx += 1

                if split_idx > 0:
                    # Добавляем часть текста
                    current_chunk += text[:split_idx]
                    text = text[split_idx:]

                # Закрываем чанк
                chunks.append(current_chunk + closing_markup)

                # Начинаем новый чанк
                current_chunk = get_opening_str(tag_stack)

    # Добавляем последний чанк, если есть
    if current_chunk:
        chunks.append(current_chunk + get_closing_str(tag_stack))

    # Фильтрация пустых чанков (иногда возникают из-за переносов)
    return [c for c in chunks if c and c != get_opening_str([])]


if __name__ == "__main__":
    # Пример, демонстрирующий "умное" разбиение и сохранение тегов
    LONG_TEXT = (
        "<b>Заголовок</b>\n"
        "В этом тексте мы проверим, как функция справляется с "
        "<pre><code class='python'>вложенными тегами</code></pre> "
        "и длинными строками, которые не должны разрываться посередине слова, "
        "если это возможно. "
        "Также проверим <a href='https://google.com'>ссылку,"
        " которая может попасть на границу</a> разрыва."
    )

    # Используем маленький лимит (200), чтобы форсировать разделение
    print(f"Исходная длина: {len(LONG_TEXT)} символов.\n")

    parts = split_html_message(LONG_TEXT, max_chars=200)

    for i, part in enumerate(parts, 1):
        print(f"--- Чанк {i} (длина {len(part)}) ---")
        print(part)
        print("-" * 30)
