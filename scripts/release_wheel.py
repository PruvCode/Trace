"""Record or check the identity of the single wheel shared by CI jobs."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from hashlib import file_digest
import json
import os
from pathlib import Path
from zipfile import ZipFile


def wheel_info(directory: Path) -> tuple[Path, str, str]:
    wheels = list(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected one wheel in {directory}, found {len(wheels)}")
    wheel = wheels[0]
    with ZipFile(wheel) as archive:
        metadata_path, = (name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
    version = metadata["Version"]
    if metadata["Name"] != "trace" or not version or wheel.name != f"trace-{version}-py3-none-any.whl":
        raise ValueError(f"unexpected TRACE wheel name or metadata for {wheel.name}")
    with wheel.open("rb") as stream:
        return wheel, version, file_digest(stream, "sha256").hexdigest()


def build_info(wheel: Path, version: str, sha256: str) -> dict[str, str]:
    ref = os.environ["GITHUB_REF"]
    if ref.startswith("refs/tags/") and ref != f"refs/tags/v{version}":
        raise ValueError(f"tag {ref.removeprefix('refs/tags/')} does not match wheel version {version}")
    return {
        "filename": wheel.name,
        "version": version,
        "sha256": sha256,
        "commit": os.environ["GITHUB_SHA"],
        "ref": ref,
        "workflow_run": f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
        f"/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    }


def record(directory: Path) -> None:
    """Write checksums and build metadata for the release artifact."""
    wheel, version, sha256 = wheel_info(directory)
    info = build_info(wheel, version, sha256)
    (directory / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    (directory / "SHA256SUMS").write_text(f"{sha256}  {wheel.name}\n", encoding="utf-8")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"sha256={sha256}\n")
        output.write(f"version={version}\n")


def verify(directory: Path) -> None:
    """Compare the downloaded wheel with the build job's expected fingerprint."""
    wheel, version, sha256 = wheel_info(directory)
    if sha256 != os.environ["TRACE_WHEEL_SHA256"]:
        raise ValueError(f"wheel checksum mismatch for {wheel.name}")
    info = build_info(wheel, version, sha256)
    recorded = json.loads((directory / "build-info.json").read_text(encoding="utf-8"))
    checksum_line = (directory / "SHA256SUMS").read_text(encoding="utf-8")
    if recorded != info or checksum_line != f"{sha256}  {wheel.name}\n":
        raise ValueError(f"release artifact metadata does not match {wheel.name} and this workflow run")
    print(f"Verified {wheel.name} at {info['commit']} ({sha256})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record", "verify"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    action = record if args.action == "record" else verify
    action(args.directory)


if __name__ == "__main__":
    main()
