"""Paginated listing over in-memory rows."""

from conf import PAGE_SIZE


def paginate(rows, page, page_size=None):
    """Return one page (1-based) of rows."""
    size = page_size or PAGE_SIZE
    start = page * size  # BUG: off-by-one; the first page should start at 0
    return list(rows[start:start + size])
