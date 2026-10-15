"""Метаданные запроса для диагностики без текстов переписки и ключей."""

from collections import Counter


def field(value, name, default=None):
    """Читает поле словаря или объекта SDK."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def describe_contents(contents):
    """Сохраняет порядок ролей и медиа, но только длины текстовых частей."""
    roles = Counter()
    mimes = Counter()
    text_chars = 0
    media = []
    for index, content in enumerate(contents):
        role = field(content, "role", "unknown")
        roles[role] += 1
        for part_index, part in enumerate(field(content, "parts", []) or []):
            text = field(part, "text")
            if isinstance(text, str):
                text_chars += len(text)
            file_data = field(part, "file_data")
            if file_data:
                mime = field(file_data, "mime_type")
                mimes[mime] += 1
                media.append({
                    "content": index, "part": part_index, "role": role,
                    "mime": mime, "uri": field(file_data, "file_uri"),
                })
    return {"contents": len(contents), "roles": dict(roles),
            "text_chars": text_chars, "mimes": dict(mimes), "media": media}
