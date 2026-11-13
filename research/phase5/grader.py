"""Execute candidate Python with a read-only WASI standard library and no host workspace."""

from __future__ import annotations

import ast
import secrets
from pathlib import Path

import wasmtime

MAX_SOURCE = 6000
FUEL = 210_000_000
MEMORY_BYTES = 128 * 1024 * 1024


class Grader:
    def __init__(self, root: Path):
        self.library = str((root / "lib").resolve(strict=True))
        config = wasmtime.Config()
        config.consume_fuel = True
        self.engine = wasmtime.Engine(config)
        self.module = wasmtime.Module.from_file(self.engine, root / "python.wasm")
        self.linker = wasmtime.Linker(self.engine)
        self.linker.define_wasi()

    def run(self, script: str) -> tuple[bool, bytes]:
        stdout = bytearray()

        def capture(data):
            stdout.extend(data[:max(0, 1024 - len(stdout))])
            return len(data)

        wasi = wasmtime.WasiConfig()
        wasi.argv = ["python", "-B", "-S", "-c", script]
        wasi.env = [("PYTHONHOME", "/")]
        wasi.stdout_custom = capture
        wasi.preopen_dir(self.library, "/lib", wasmtime.DirPerms.READ_ONLY, wasmtime.FilePerms.READ_ONLY)
        with wasmtime.Store(self.engine) as store:
            store.set_wasi(wasi)
            store.set_fuel(FUEL)
            store.set_limits(memory_size=MEMORY_BYTES)
            try:
                instance = self.linker.instantiate(store, self.module)
                instance.exports(store)["_start"](store)
            except wasmtime.ExitTrap as exc:
                return exc.code == 0, bytes(stdout)
            except wasmtime.Trap:
                return False, bytes(stdout)
        return True, bytes(stdout)

    def evaluate(self, source: str, check: str) -> bool:
        if not source.strip() or len(source) > MAX_SOURCE:
            return False
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            return False
        # Ordinary fixture solutions need function definitions and at most time.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name != "time" for alias in node.names):
                return False
            if isinstance(node, ast.ImportFrom) and (node.level or node.module != "time"):
                return False
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                return False
            if isinstance(node, ast.Name) and node.id in {"__builtins__", "__import__", "eval", "exec", "open",
                                                        "compile", "getattr", "setattr", "globals", "locals",
                                                        "exit", "quit", "input", "breakpoint"}:
                return False
        token = secrets.token_hex(16)
        ok, output = self.run(f"source = {source!r}\n{check}\nprint({token!r})")
        return ok and output == (token + "\n").encode()

    def verify_boundary(self, canary: Path) -> None:
        script = f"""
import os
assert "TRACE_BENCH_HOST_SENTINEL" not in os.environ
for path, mode in (({str(canary)!r}, "rb"), ("/outside.txt", "rb"), ("/lib/phase5-probe", "wb")):
    try:
        with open(path, mode):
            pass
    except OSError:
        continue
    raise AssertionError("unexpected filesystem capability")
print("contained")
"""
        assert self.run(script) == (True, b"contained\n")
        assert self.run("while True: pass")[0] is False
