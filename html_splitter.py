import re


def split_html_message(html: str, max_chars: int = 4000) -> list[str]:
    """
    Splits HTML into chunks, ensuring tags are closed and reopened correctly.
    max_chars is set to 4000 by default to leave a buffer for the added
    closing/opening tags.
    """
    if len(html) <= max_chars:
        return [html]

    # Regex to find HTML tags vs text content
    tokens = re.split(r"(<[^>]+>)", html)

    chunks = []
    current_chunk = ""
    tag_stack = []  # To keep track of open tags (the full tag string)

    def get_tag_name(tag_token):
        # Extracts "a" from " <a href='...'>" or "b" from "<b>"
        match = re.search(r"</?([^\s>]+)", tag_token)
        return match.group(1) if match else ""

    for token in tokens:
        if not token:
            continue

        # Check if adding this token (plus potential closing tags) exceeds limit
        # We estimate closing tags length by looking at the tag stack
        closing_tags_needed = "".join(
            [f"</{get_tag_name(t)}>" for t in reversed(tag_stack)]
        )

        if len(current_chunk) + len(token) + len(closing_tags_needed) > max_chars:
            # If the token itself is too long (massive text block), we must force split it
            if not token.startswith("<") and len(token) > max_chars:
                # Split text block by characters safely
                remaining_space = (
                    max_chars - len(current_chunk) - len(closing_tags_needed)
                )
                current_chunk += token[:remaining_space]
                token = token[remaining_space:]

            # Close the current chunk
            current_chunk += closing_tags_needed
            chunks.append(current_chunk)

            # Start new chunk and re-open the stack
            current_chunk = "".join(tag_stack)
            # If we split a massive text block, we continue with the remainder
            # Otherwise we just continue loop with the current token

        if token.startswith("<"):
            tag_name = get_tag_name(token)
            if token.startswith("</"):
                # Closing tag: remove from stack
                if tag_stack and get_tag_name(tag_stack[-1]) == tag_name:
                    tag_stack.pop()
            elif not token.endswith("/>") and tag_name not in ["br"]:
                # Opening tag: add to stack
                tag_stack.append(token)

        current_chunk += token

    if current_chunk:
        # Add final closing tags if any remain in stack
        closing_tags_needed = "".join(
            [f"</{get_tag_name(t)}>" for t in reversed(tag_stack)]
        )
        chunks.append(current_chunk + closing_tags_needed)

    return chunks
