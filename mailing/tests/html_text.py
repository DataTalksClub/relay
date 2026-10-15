"""Template output helpers for tests."""

import re

DATA_SPAN = re.compile(r'<span class="data-(?:num|time)">(.*?)</span>', re.S)


def data_text(content):
    """Content with the mono .data-num/.data-time wrappers removed, so tests
    can assert on the words a person reads ("3 members", "Page 1 of 2")."""
    text = content.decode() if isinstance(content, bytes) else content
    text = DATA_SPAN.sub(r"\1", text)
    return text.encode() if isinstance(content, bytes) else text
