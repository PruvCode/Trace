"""Parser records shared by TRACE's structural index and graph."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import NamedTuple
import zlib

MAX_FRAGMENT_BYTES = 8 * 1024 * 1024


class Definition(NamedTuple):
    name: str
    kind: str
    line: int
    column: int
    end_line: int
    end_column: int


class Observation(NamedTuple):
    kind: str
    label: str
    line: int
    column: int
    module: str | None = None
    symbol: str | None = None
    level: int = 0


@dataclass
class Fragment:
    definitions: list[Definition]
    symbols: list[tuple[str, str, int, str]]
    relationships: list[tuple[str, str, str, int]]
    exports: dict[str, list[tuple[str, int]]]
    observations: list[Observation]

    def encode(self) -> bytes:
        # A caller name can recur at every call site in a large function.
        callers = {}
        relationships = [(callers.setdefault(src, len(callers)), kind, target, line)
                         for src, kind, target, line in self.relationships]
        record = {**vars(self), "callers": list(callers), "relationships": relationships}
        return zlib.compress(json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("ascii"), 3)

    @classmethod
    def decode(cls, data: bytes) -> Fragment:
        decoder = zlib.decompressobj()
        payload = decoder.decompress(data, MAX_FRAGMENT_BYTES)
        if not decoder.eof or decoder.unused_data:
            raise ValueError("invalid structural cache entry")
        record = json.loads(payload)
        callers = record["callers"]
        relationships = [(callers[src], kind, target, line) for src, kind, target, line in record["relationships"]]
        return cls(
            [Definition(*row) for row in record["definitions"]], record["symbols"], relationships,
            record["exports"], [Observation(*row) for row in record["observations"]],
        )
