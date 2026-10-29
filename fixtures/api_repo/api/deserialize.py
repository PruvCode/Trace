"""Inbound deserialization: wire format -> internal records."""


def from_wire(payload):
    """Convert a wire payload to an internal user record."""
    return {"user_id": payload["userId"], "name": payload["name"]}
