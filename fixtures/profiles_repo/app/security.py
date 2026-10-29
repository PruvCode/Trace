"""Security policy: canonical input sanitization."""

import re

_TAG_RE = re.compile(r"<[^>]*>")


def sanitize(value):
    """Strip markup and surrounding whitespace from user-supplied text."""
    return _TAG_RE.sub("", value or "").strip()
