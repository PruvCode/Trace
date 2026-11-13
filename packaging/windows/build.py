"""Build the Windows x64 preview from a reviewed wheel and pinned upstream assets."""

import argparse
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PYTHON = "cpython-3.11.15+20260718-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
OPENCODE = "opencode-windows-x64-baseline.zip"
CHECKSUMS = {
    PYTHON: "a48c2dbe832319f61aa8557c9900caec70f7fed0cbee391a4c9ff9f98b50222d",
    OPENCODE: "3acca208ad79add59aed13d07b7e08ddf24e0adbbb7f44a9131408d7b3ca67cd",
    "OpenCode-LICENSE": "625f0f619133f89bbbb2abe37369613dfa1885eba1e50d02170deb62bb42cb6b",
}


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def build(args, stage):
    for name, digest in CHECKSUMS.items():
        if sha256(args.assets / name) != digest:
            raise ValueError(f"Upstream checksum mismatch for {name}")
    bundle = stage / "bundle"
    bundle.mkdir()
    with tarfile.open(args.assets / PYTHON, "r:gz") as archive:
        archive.extractall(bundle, filter="data")
    with zipfile.ZipFile(args.assets / OPENCODE) as archive:
        archive.extract("opencode.exe", bundle / "opencode")

    notices = bundle / "licenses"
    notices.mkdir()
    shutil.copyfile(ROOT / "LICENSE", notices / "TRACE-LICENSE")
    shutil.copyfile(args.assets / "OpenCode-LICENSE", notices / "OpenCode-LICENSE")
    (notices / "Sources.txt").write_text(
        "TRACE runtime dependencies retain their wheel metadata and license files.\n"
        "Python notices are in python/LICENSE.txt.\n"
        "Python build: https://github.com/astral-sh/python-build-standalone/releases/tag/20260718\n"
        "OpenCode build: https://github.com/anomalyco/opencode/releases/tag/v2.2.5\n",
        encoding="utf-8",
    )

    bin_dir = bundle / "bin"
    bin_dir.mkdir()
    for name in ("trace.cmd", "trace-opencode.cmd"):
        (bin_dir / name).write_text((HERE / name).read_text(encoding="ascii"), encoding="ascii", newline="\r\n")
    runtime = bundle / "python" / "python.exe"
    env = dict(os.environ, PIP_CONFIG_FILE=os.devnull)
    subprocess.run(
        [str(runtime), "-IB", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
         "--no-cache-dir", "--no-index", "--no-compile", "--no-warn-script-location", "--find-links", str(args.wheelhouse),
         "-c", str(ROOT / "constraints.txt"), str(args.wheel)],
        cwd=stage, env=env, check=True,
    )
    subprocess.run([str(runtime), "-IB", "-m", "pip", "check"], cwd=stage, env=env, check=True)
    subprocess.run([str(runtime), "-IB", "-m", "trace_memory.cli", "--version"], cwd=stage, check=True)
    subprocess.run([str(bundle / "opencode" / "opencode.exe"), "--version"], cwd=stage, check=True)

    payload = stage / "trace-payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for path in sorted(bundle.rglob("*")):
            relative = path.relative_to(bundle)
            # pip-generated launchers and origin metadata contain paths from the build machine.
            if path.is_file() and relative.parts[:2] != ("python", "Scripts") and path.name != "direct_url.json":
                archive.write(path, relative)
    installer = (HERE / "Install-TRACE.ps1").read_text(encoding="ascii")
    assert installer.count("@PAYLOAD_SHA256@") == 1
    installer = installer.replace("@PAYLOAD_SHA256@", sha256(payload))
    with zipfile.ZipFile(args.output, "x", zipfile.ZIP_STORED) as release:
        release.write(payload, payload.name)
        release.writestr("Install-TRACE.ps1", installer.replace("\n", "\r\n"))
        release.write(HERE / "START-HERE.txt", "START-HERE.txt")
    print(f"{sha256(args.output)}  {args.output.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("assets", "wheel", "wheelhouse", "output"):
        parser.add_argument(f"--{name}", type=lambda value: Path(value).resolve(), required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Build the Windows package on Windows x64")
    with tempfile.TemporaryDirectory(prefix="trace-release-") as directory:
        build(args, Path(directory))


if __name__ == "__main__":
    main()
