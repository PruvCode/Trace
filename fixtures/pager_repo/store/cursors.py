"""Opaque pagination cursors (post-rollout format)."""


def encode_cursor(offset):
    """Encode a row offset as an opaque cursor."""
    return f"cur_{offset}"


def decode_cursor(cursor):
    """Decode a cursor back to a row offset."""
    if not isinstance(cursor, str) or not cursor.startswith("cur_"):
        raise ValueError("bad cursor")
    return int(cursor[len("cur_"):])
