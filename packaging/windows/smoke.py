"""Exercise the Windows bundle without changing the current user's PATH or history."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--fetch-git", action="store_true", help="exercise the Git-missing branch")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with assertions enabled")
    root = args.scratch.resolve()
    root.mkdir()
    files = root / "bundle"
    with zipfile.ZipFile(args.bundle) as bundle:
        bundle.extractall(files)
    installer = files / "Install-TRACE.ps1"
    source = installer.read_text(encoding="ascii")
    checksum, = re.findall(r"\$expected = '([a-f0-9]{64})'", source)
    name = "preview-" + checksum[:12]

    windows = Path(os.environ["SystemRoot"])
    env = dict(os.environ)
    path = [str(windows / "System32"), str(windows)]
    if not args.fetch_git:
        path.append(str(Path(shutil.which("git.exe")).parent))
    env["PATH"] = os.pathsep.join(path)
    powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"

    def install(script, target, success=True):
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(script), "-NoPath", "-InstallRoot", str(target)],
            cwd=root, env=env, capture_output=True, text=True, timeout=240,
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result.stdout + result.stderr

    install_root = root / "Local app data"
    install(installer, install_root)
    installed = install_root / name
    assert installed.is_dir() and (installed / "bundle.sha256").read_text() == checksum
    assert (installed / "git/cmd/git.exe").is_file() == args.fetch_git
    install(installer, install_root)
    assert list(install_root.iterdir()) == [installed]

    wrong = files / "Wrong-Checksum.ps1"
    wrong.write_text(source.replace(checksum, "0" * 64), encoding="ascii")
    bad_target = root / "bad"
    assert "checksum did not match" in install(wrong, bad_target, False)
    assert not bad_target.exists()

    foreign = root / "foreign" / name
    foreign.mkdir(parents=True)
    sentinel = foreign / "owned-by-another-app"
    sentinel.write_text("keep", encoding="utf-8")
    assert "already contains other files" in install(installer, foreign.parent, False)
    assert sentinel.read_text(encoding="utf-8") == "keep"

    if args.fetch_git:
        env["PATH"] += os.pathsep + str(installed / "git/cmd")
    python = installed / "python/python.exe"
    binary = installed / "opencode/opencode.exe"
    smoke = subprocess.run(
        [sys.executable, "-m", "research.phase6.native_smoke", "--major", "2", "--managed",
         "--python", str(python), "--binary", str(binary), "--scratch", str(root / "native")],
        env=env, capture_output=True, encoding="utf-8", check=True, timeout=300,
    )
    native = json.loads(smoke.stdout)
    assert native["native_evidence_round_trip"] and native["backup_restore_verified"]
    print(json.dumps({"install": True, "repeat_install": True, "checksum_rejection": True,
                      "foreign_files_preserved": True, "private_git": args.fetch_git, "native": native}))


if __name__ == "__main__":
    main()
