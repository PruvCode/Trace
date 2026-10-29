"""Outbound serialization: internal records -> wire format."""


def to_wire(user):
    """Convert an internal user record to its wire representation."""
    return {"user_id": user["user_id"], "name": user["name"]}  # BUG: snake_case wire keys
