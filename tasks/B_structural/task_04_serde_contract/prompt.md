# Repair the serializer/deserializer contract

Records serialized with `to_wire()` can no longer be read back: the
serializer and the deserializer disagree about the wire representation
of user records, so a round trip through the wire format corrupts field
names.

Change the code so that:

- `to_wire()` emits the canonical wire representation the service
  exchanges with its peers,
- `from_wire(to_wire(user))` returns the original record,
- payloads already on the wire keep decoding correctly.

You are done when serialization round-trips and matches the wire
format the rest of the service speaks. Do not just claim the fix
works — the benchmark checks the behavior mechanically.
