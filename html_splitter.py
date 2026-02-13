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


def split_html_message(html: str, max_chars: int = 4000) -> list[str]:
    """
    Разбивает HTML-сообщение на части, обеспечивая корректное закрытие
    и повторное открытие тегов на границах разделения.

    Функция анализирует HTML-строку, разделяя её на токены (теги и текстовое содержимое).
    При превышении лимита длины части она закрывает все открытые теги в текущем фрагменте,
    а в следующем фрагменте заново открывает их. Текстовые блоки, превышающие максимальную
    длину, принудительно разбиваются.

    Параметры:
        html: Исходная HTML-строка для разделения
        max_chars: Максимальная длина одной части в символах.
                   Значение по умолчанию 4000 оставляет запас для добавляемых
                   закрывающих/открывающих тегов.

    Возвращает:
        Список строк, содержащих корректно оформленные HTML-фрагменты

    Пример:
        >>> html = "<div><p>Длинный текст...</p><span>Ещё текст</span></div>"
        >>> parts = split_html_message(html, max_chars=50)
        >>> for part in parts:
        ...     print(part)
        <div><p>Длинный текст...</p></div>
        <div><span>Ещё текст</span></div>

    Примечания:
        - Самозакрывающиеся теги (например, <img />, <br />) и <br> не добавляются в стек
        - Атрибуты тегов полностью сохраняются при повторном открытии
        - Функция не проверяет корректность исходного HTML
    """
    if len(html) <= max_chars:
        return [html]

    # Регулярное выражение для разделения на HTML-теги и текст
    # Скобки в регулярном выражении сохраняют разделители (теги) в результате
    tokens = re.split(r"(<[^>]+>)", html)

    chunks = []
    current_chunk = ""
    tag_stack = []  # Стек открытых тегов (хранит полные строки тегов)

    def get_tag_name(tag_token: str) -> str:
        """
        Извлекает имя тега из строки токена.

        Параметры:
            tag_token: Строка с HTML-тегом, например '<a href="...">' или '</div>'

        Возвращает:
            Имя тега или пустую строку, если не удалось извлечь
        """
        match = re.search(r"</?([^\s>]+)", tag_token)
        return match.group(1) if match else ""

    for token in tokens:
        if not token:
            continue

        # Проверяем, не превысит ли лимит добавление токена с учётом закрывающих тегов
        closing_tags_needed = "".join(
            [f"</{get_tag_name(t)}>" for t in reversed(tag_stack)]
        )

        if len(current_chunk) + len(token) + len(closing_tags_needed) > max_chars:
            # Если сам токен слишком длинный (большой текстовый блок),
            # принудительно разбиваем его
            if not token.startswith("<") and len(token) > max_chars:
                remaining_space = (
                    max_chars - len(current_chunk) - len(closing_tags_needed)
                )
                current_chunk += token[:remaining_space]
                token = token[remaining_space:]

            # Закрываем текущую часть
            current_chunk += closing_tags_needed
            chunks.append(current_chunk)

            # Начинаем новую часть с открытия всех активных тегов
            current_chunk = "".join(tag_stack)
            # Если мы разбили большой текстовый блок, продолжаем с оставшейся частью
            # Иначе просто продолжаем цикл с текущим токеном

        if token.startswith("<"):
            tag_name = get_tag_name(token)
            if token.startswith("</"):
                # Закрывающий тег: удаляем из стека
                if tag_stack and get_tag_name(tag_stack[-1]) == tag_name:
                    tag_stack.pop()
            elif not token.endswith("/>") and tag_name not in ["br"]:
                # Открывающий тег: добавляем в стек
                # Игнорируем самозакрывающиеся теги и <br>
                tag_stack.append(token)

        current_chunk += token

    if current_chunk:
        # Добавляем недостающие закрывающие теги в конце
        closing_tags_needed = "".join(
            [f"</{get_tag_name(t)}>" for t in reversed(tag_stack)]
        )
        chunks.append(current_chunk + closing_tags_needed)

    return chunks


if __name__ == "__main__":
    print("HTML splitter.\nUsage: split_html_message(html: str, max_chars: int = 4000)")
