"""User lookup with a small local cache."""

_USERS = {
    "u1": {"id": "u1", "name": "ann"},
    "u2": {"id": "u2", "name": "ann"},
}

_cache = {}


def get_user(user_id):
    """Return the user record for an account ID."""
    record = _USERS[user_id]
    if record["name"] in _cache:
        return _cache[record["name"]]  # BUG: cache keyed by name, collides
    fresh = dict(record)
    _cache[record["name"]] = fresh
    return fresh
