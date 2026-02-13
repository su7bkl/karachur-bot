"""
Модуль для разделения больших HTML-сообщений на части.

Предоставляет функцию split_html_message(), которая разбивает HTML-строку на фрагменты
заданной максимальной длины, сохраняя корректную вложенность тегов.
На границах разделения все открытые теги автоматически закрываются в текущем фрагменте
и повторно открываются в следующем.

Особенности:
    - Корректно обрабатывает вложенные теги любой глубины
    - Поддерживает атрибуты тегов
    - Игнорирует самозакрывающиеся теги и <br>
    - Разбивает длинные текстовые блоки без тегов
    - Оставляет буфер для служебных тегов (по умолчанию 4000 символов)

Пример использования:
    >>> from html_splitter import split_html_message
    >>>
    >>> long_html = "<div><p>Очень длинный текст...</p><span>Ещё текст</span></div>"
    >>> parts = split_html_message(long_html, max_chars=100)
    >>> for i, part in enumerate(parts, 1):
    ...     print(f"Часть {i}:\\n{part}\\n")
"""

import re


def split_html_message(
    html: str, max_chars: int = 4000, min_chunk_size: int = 200
) -> list[str]:
    """
    Разбивает HTML-сообщение на части, обеспечивая корректное закрытие
    и повторное открытие тегов на границах разделения.

    Параметры:
        html: Исходная HTML-строка для разделения.
        max_chars: Максимальная длина одной части.
        min_chunk_size: Минимальный желаемый размер текста при разделении.
                        Предотвращает создание крошечных текстовых "огрызков".

    Возвращает:
        Список строк, содержащих корректно оформленные HTML-фрагменты.
    """
    if len(html) <= max_chars:
        return [html]

    tokens = re.split(r"(<[^>]+>)", html)
    chunks = []
    current_chunk = ""
    tag_stack = []

    # Список стандартных самозакрывающихся тегов HTML5
    VOID_ELEMENTS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def get_tag_name(tag_token: str) -> str:
        """Извлекает имя тега в нижнем регистре."""
        match = re.search(r"</?([^\s/>]+)", tag_token)
        return match.group(1).lower() if match else ""

    def get_closing_tags(stack: list[str]) -> str:
        """Генерирует строку с закрывающими тегами для текущего стека."""
        return "".join([f"</{get_tag_name(t)}>" for t in reversed(stack)])

    def close_and_start_new_chunk():
        """Закрывает текущий чанк, сохраняет его и начинает новый с открытыми тегами."""
        nonlocal current_chunk
        closing_tags = get_closing_tags(tag_stack)
        chunks.append(current_chunk + closing_tags)
        current_chunk = "".join(tag_stack)

    for token in tokens:
        if not token:
            continue

        if token.startswith("<"):
            tag_name = get_tag_name(token)
            is_closing = token.startswith("</")
            is_self_closing = token.endswith("/>") or tag_name in VOID_ELEMENTS

            # Имитируем стек, чтобы понять будущую длину
            temp_stack = tag_stack.copy()
            if is_closing:
                if temp_stack and get_tag_name(temp_stack[-1]) == tag_name:
                    temp_stack.pop()
            elif not is_self_closing:
                temp_stack.append(token)

            future_closing = get_closing_tags(temp_stack)
            predicted_len = len(current_chunk) + len(token) + len(future_closing)
            open_tags_len = len("".join(tag_stack))

            # Если добавление тега превысит лимит, закрываем чанк ДО его добавления.
            # Но если текущий чанк слишком мал (< min_chunk_size), мы разрешаем
            # ему слегка превысить max_chars, чтобы не плодить пустые чанки.
            if predicted_len > max_chars and len(current_chunk) >= min_chunk_size:
                if (
                    len(current_chunk) > open_tags_len
                ):  # Убеждаемся, что в чанке есть контент
                    close_and_start_new_chunk()

            current_chunk += token
            tag_stack = temp_stack

        else:
            # Обработка текстового блока
            closing_tags = get_closing_tags(tag_stack)

            while len(current_chunk) + len(token) + len(closing_tags) > max_chars:
                available_space = max_chars - len(current_chunk) - len(closing_tags)
                open_tags_len = len("".join(tag_stack))

                # Если места осталось мало (меньше минимального чанка) и чанк уже содержит текст,
                # лучше закрыть этот чанк пораньше и перенести слово целиком в следующий.
                if available_space <= 0 or (
                    available_space < min_chunk_size
                    and len(current_chunk) > open_tags_len
                ):
                    close_and_start_new_chunk()
                    closing_tags = get_closing_tags(tag_stack)
                    continue

                # Ищем оптимальное место для разрезания текста (по пробелу/переносу строки)
                split_idx = available_space
                if len(token) > available_space:
                    last_space = max(
                        token.rfind(" ", 0, available_space + 1),
                        token.rfind("\n", 0, available_space + 1),
                    )
                    if last_space > 0:
                        split_idx = last_space

                    # Защита от разрезания HTML-сущностей (например, &nbsp;)
                    entity_amp = token.rfind("&", max(0, split_idx - 10), split_idx)
                    entity_semi = token.rfind(";", max(0, split_idx - 10), split_idx)
                    if (
                        entity_amp > entity_semi
                    ):  # Значит сущность открылась, но не закрылась
                        split_idx = entity_amp

                current_chunk += token[:split_idx]
                token = token[split_idx:].lstrip(
                    " \n"
                )  # Убираем ведущие пробелы у остатка

                close_and_start_new_chunk()
                closing_tags = get_closing_tags(tag_stack)

            if token:
                current_chunk += token

    # Сохраняем оставшийся кусок, если в нем есть хоть что-то кроме пустых тегов
    open_tags_len = len("".join(tag_stack))
    if len(current_chunk) > open_tags_len:
        chunks.append(current_chunk + get_closing_tags(tag_stack))

    return chunks


if __name__ == "__main__":
    test_html = "<div><p>Очень длинный текст, который должен быть красиво и правильно разбит на части без потери смысла и тегов...</p><span>Ещё текст</span></div>"

    print("--- Тест на разбиение текста ---")
    parts = split_html_message(test_html, max_chars=120, min_chunk_size=30)
    for i, part in enumerate(parts, 1):
        print(f"Часть {i} (длина {len(part)}):\n{part}\n")
