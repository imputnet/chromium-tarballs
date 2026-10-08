"""Prepare, package and verify on a GitHub-hosted runner."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    host = os.environ.get("PRODUCER_HOST") or "linux-x64"
    version = os.environ["VERSION"]
    workspace = Path(os.environ["PRODUCER_WORKSPACE"])
    cache = os.environ["PRODUCER_CACHE"]
    output = Path(os.environ["PRODUCER_OUTPUT"])
    workspace.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "scripts/chromium_tarballs.py"]
    subprocess.run(
        command
        + [
            "prepare",
            "--version",
            version,
            "--host",
            host,
            "--workspace",
            str(workspace),
            "--cache",
            cache,
        ],
        check=True,
    )
    package_args = ["package", "--workspace", str(workspace), "--output", str(output)]
    if host != "linux-x64":
        package_args.extend(["--base-manifest", "linux-dist/manifest.json"])
    subprocess.run(command + package_args, check=True)
    subprocess.run(
        command + ["verify", "--manifest", str(output / "manifest.json")], check=True
    )


if __name__ == "__main__":
    main()
