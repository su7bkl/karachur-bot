"""Конвертер Markdown → упрощённый HTML для Telegram.

Функции:
- markdown_to_telegram_html(md): превращает Markdown в HTML, совместимый с ограниченным набором тегов Telegram.
- clean_for_telegram(soup): очищает дерево от неподдерживаемых тегов и небезопасных атрибутов.

Особенности:
- Списки (<ul>/<ol>) разворачиваются в текст с маркерами (•) или нумерацией.
- Параграфы <p> заменяются на двойные переводы строк.
- Поддерживаются только теги: b, i, u, s, a, code, pre, br, tg-spoiler.
- Атрибуты сохраняются только для ссылок (href).
"""

from markdown import markdown
from bs4 import BeautifulSoup

ALLOWED_TAGS = {'b', 'i', 'u', 's', 'a', 'code', 'pre', 'br', 'tg-spoiler'}
TELEGRAM_TAG_MAP = {
    'strong': 'b',
    'b': 'b',
    'em': 'i',
    'i': 'i',
    'u': 'u',
    'strike': 's',
    's': 's',
    'code': 'code',
    'pre': 'pre',
    'a': 'a',
    'br': 'br',
    'tg-spoiler': 'tg-spoiler'
}


def clean_for_telegram(soup: BeautifulSoup) -> BeautifulSoup:
    """Оставляет только разрешённые Telegram HTML-теги и очищает атрибуты."""
    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
        else:
            if tag.name == 'a':
                href = tag.get('href', '')
                tag.attrs = {'href': href}
            else:
                tag.attrs = {}
    return soup


def markdown_to_telegram_html(md: str) -> str:
    """Конвертирует Markdown в безопасный HTML для Telegram."""
    raw_html = markdown(md, extensions=['fenced_code', 'tables'])
    soup = BeautifulSoup(raw_html, 'html.parser')

    # Преобразуем списки в текст
    for ul in soup.find_all(['ul', 'ol']):
        is_ordered = ul.name == 'ol'
        idx = 1
        lines = []
        for li in ul.find_all('li', recursive=False):
            text = ''.join(str(c) for c in li.contents)
            if is_ordered:
                lines.append(f"{idx}. {text}")
            else:
                lines.append(f"• {text}")
            idx += 1
        ul.replace_with(BeautifulSoup('\n'.join(lines), 'html.parser'))

    # Убираем <p>, заменяя на переносы
    for p in soup.find_all('p'):
        p.insert_before('\n\n')
        p.unwrap()

    # Маппим поддерживаемые теги
    for tag in soup.find_all(True):
        if tag.name in TELEGRAM_TAG_MAP:
            tag.name = TELEGRAM_TAG_MAP[tag.name]

    # Фильтрация для Telegram
    soup = clean_for_telegram(soup)

    return str(soup).strip()


if __name__ == "__main__":
    print("Markdown to Telegram HTML converter.\nUsage: markdown_to_telegram_html(md_string)")
